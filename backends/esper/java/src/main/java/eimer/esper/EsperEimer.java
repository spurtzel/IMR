package eimer.esper;

import com.espertech.esper.common.client.EPCompiled;
import com.espertech.esper.common.client.EventBean;
import com.espertech.esper.common.client.configuration.Configuration;
import com.espertech.esper.common.client.fireandforget.EPFireAndForgetQueryResult;
import com.espertech.esper.compiler.client.CompilerArguments;
import com.espertech.esper.compiler.client.EPCompiler;
import com.espertech.esper.compiler.client.EPCompilerProvider;
import com.espertech.esper.runtime.client.EPDeployment;
import com.espertech.esper.runtime.client.EPFireAndForgetService;
import com.espertech.esper.runtime.client.EPRuntime;
import com.espertech.esper.runtime.client.EPRuntimeProvider;
import com.espertech.esper.runtime.client.EPStatement;

import java.io.IOException;
import java.lang.management.ManagementFactory;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

public final class EsperEimer {
    private static String esperVersion() {
        try (java.io.InputStream in = EsperEimer.class.getResourceAsStream("/META-INF/maven/com.espertech/esper-runtime/pom.properties")) {
            if (in != null) {
                java.util.Properties props = new java.util.Properties();
                props.load(in);
                String v = props.getProperty("version");
                if (v != null) {
                    return v;
                }
            }
        } catch (IOException ignored) {
            // fall through
        }
        String v = com.espertech.esper.runtime.client.EPRuntimeProvider.class.getPackage().getImplementationVersion();
        return v != null ? v : "unknown";
    }

    /** the wall in flight: Esper cannot be interrupted, so on a deadline the partial record is written with
     *  status dnf and the JVM halts (the driver reads the record). Armed around every timed region. */
    private static final class Watchdog {
        private final RunRecord record;
        private final Path out;
        private volatile long deadline = Long.MAX_VALUE;

        Watchdog(RunRecord record, Path out) {
            this.record = record;
            this.out = out;
            Thread t = new Thread(() -> {
                while (true) {
                    try {
                        Thread.sleep(100);
                    } catch (InterruptedException e) {
                        return;
                    }
                    if (System.nanoTime() > deadline) {
                        record.status = "dnf";
                        record.notes.append("Wall exceeded in flight (batch ").append(record.perBatch.size() + 1)
                                .append("): the partial record was written by the watchdog and the JVM halted.\n");
                        try {
                            writeRecord(out, record);
                        } catch (IOException ignored) {
                            // nothing better to do
                        }
                        System.err.println("watchdog: wall exceeded, halting");
                        Runtime.getRuntime().halt(3);
                    }
                }
            }, "wall-watchdog");
            t.setDaemon(true);
            t.start();
        }

        void arm(long nanosFromNow) {
            deadline = System.nanoTime() + Math.max(0L, nanosFromNow);
        }

        void disarm() {
            deadline = Long.MAX_VALUE;
        }
    }

    private EsperEimer() {
    }

    public static void main(String[] args) throws Exception {
        Cli cli;
        try {
            cli = Cli.parse(args);
        } catch (IllegalArgumentException ex) {
            System.err.println(ex.getMessage());
            System.err.println("usage: java -jar target/esper-eimer.jar --data <dir> --plan <plan.json> "
                    + "--arm eimer|native|none --out <record.json> --wall-s <T> --total-wall-s <T> [--final-gc 1] [--gc-samples <S>]");
            System.exit(2);
            return;
        }

        RunRecord record;
        try {
            Plan plan = Json.mapper().readValue(Files.readString(cli.plan), Plan.class);
            if (!"none".equals(cli.arm)) {
                plan.validate(cli.arm);
            }
            DataSet data = DataSet.read(cli.data, plan);
            record = switch (cli.arm) {
                case "eimer" -> runEimer(data, plan, cli);
                case "native" -> runNative(data, plan, cli);
                // "none": the data set is loaded exactly as above and nothing runs;
                // measured like the arms, it is the JVM + data baseline subtracted from every arm's working memory
                case "none" -> runNone(data);
                default -> throw new IllegalArgumentException("unknown arm: " + cli.arm);
            };
        } catch (Throwable t) {
            record = RunRecord.failed(cli.arm, t);
        }

        if (cli.finalGc) {   // memory measurement: the live set at the end of the run, after the timed section
            System.gc(); System.gc();
            Runtime rt = Runtime.getRuntime();
            record.finalLiveHeapMb = (rt.totalMemory() - rt.freeMemory()) / (1024.0 * 1024.0);
        }
        try {
            record.dataSha16 = DataSet.dataSha16(cli.data);
        } catch (Exception e) {
            record.notes.append("data sha unavailable: ").append(e).append('\n');
        }
        writeRecord(cli.out, record);
        System.out.printf(Locale.ROOT, "arm=%s status=%s total_ms=%.3f final_count=%d out=%s%n",
                record.arm, record.status, ms(record.totalNanos), record.finalTuples.size(), cli.out);
    }

    /** --gc-samples S (memory measurement): at ~S evenly spaced batch boundaries and after the last batch, two
     *  forced full collections and the live heap, outside the timed section; the record keeps the peak. */
    private static void sampleLiveHeap(RunRecord record, Cli cli, int bi, int nBatches, LinkedHashMap<String, Object> perBatch) {
        if (cli.gcSamples <= 0) {
            return;
        }
        int every = Math.max(1, nBatches / cli.gcSamples);
        if (bi % every != 0 && bi != nBatches - 1) {
            return;
        }
        System.gc(); System.gc();
        Runtime rt = Runtime.getRuntime();
        double live = (rt.totalMemory() - rt.freeMemory()) / (1024.0 * 1024.0);
        perBatch.put("live_heap_mb", Math.round(live * 10.0) / 10.0);
        record.peakLiveHeapSampledMb = Math.max(record.peakLiveHeapSampledMb, live);
    }

    private static RunRecord runNone(DataSet data) {
        RunRecord record = new RunRecord("none");
        record.totalNanos = 0L;
        // the data set must be reachable during the forced collections: measured here, the rows are touched afterwards
        System.gc(); System.gc();
        Runtime rt = Runtime.getRuntime();
        record.peakLiveHeapSampledMb = (rt.totalMemory() - rt.freeMemory()) / (1024.0 * 1024.0);
        record.notes.append("baseline: data set loaded (").append(data.rows.size()).append(" rows, ")
                .append(data.batches.size()).append(" batches), no statements; live heap after forced GC with the rows held: ")
                .append(Math.round(record.peakLiveHeapSampledMb)).append(" MB\n");
        return record;
    }

    private static RunRecord runEimer(DataSet data, Plan plan, Cli cli) throws Exception {
        RunRecord record = new RunRecord("eimer");
        record.notes.append("EIMER arm deployed plan.eimer.module as-is. Batch rows are sent first as RowEvent ")
                .append("outside the timed region, then as DriveEvent inside the timed region; one ComposeTrigger ")
                .append("fires the listener-collected compose. No create index EPL is emitted by this program.\n");

        Configuration cfg = new Configuration();
        cfg.getRuntime().getThreading().setInternalTimerEnabled(false);
        Watchdog watchdog = new Watchdog(record, cli.out);
        EPRuntime runtime = EPRuntimeProvider.getRuntime("esper-eimer-" + System.nanoTime(), cfg);
        runtime.initialize();
        EPDeployment deployment = null;
        try {
            EPCompiler compiler = EPCompilerProvider.getCompiler();
            deployment = deploy(runtime, cfg, compiler, plan.eimer.module);
            for (Plan.ViewPlan view : plan.eimer.views) {
                requireStatement(runtime, deployment, view.updateStatement);
            }
            EPStatement compose = requireStatement(runtime, deployment, plan.eimer.composeStatement);
            CandidateCollector collector = new CandidateCollector(plan.keyColumns);
            compose.addListener(collector);

            EPFireAndForgetService faf = runtime.getFireAndForgetService();
            LinkedHashMap<String, EPCompiled> residentQueries = new LinkedHashMap<>();
            for (Plan.ViewPlan view : plan.eimer.views) {
                residentQueries.put(view.name, compileQuery(runtime, cfg, compiler, "select count(*) as c from " + view.name));
            }

            Cursor cursor = new Cursor();
            for (int bi = 0; bi < data.batches.size(); bi++) {
                DataSet.Batch batch = data.batches.get(bi);
                List<DataSet.Row> batchRows = takeBatchRows(data.rows, cursor, batch);
                long ingestStart = System.nanoTime();
                for (DataSet.Row row : batchRows) {
                    runtime.getEventService().sendEventMap(row.rowEvent(), "RowEvent");
                }
                long ingestNanos = System.nanoTime() - ingestStart;

                long batchMinTs = minTs(batchRows);
                watchdog.arm(Math.min(cli.wallNanos, cli.totalWallNanos - record.totalNanos));
                long updateStart = System.nanoTime();
                for (DataSet.Row row : batchRows) {
                    runtime.getEventService().sendEventMap(row.driveEvent(batchMinTs), "DriveEvent");
                }
                long updateEnd = System.nanoTime();
                collector.clear();
                runtime.getEventService().sendEventMap(Map.of("b", batch.b), "ComposeTrigger");
                long composeEnd = System.nanoTime();
                List<long[]> candidates = collector.snapshot();
                List<long[]> selected = Selection.select(candidates);
                long selectionEnd = System.nanoTime();

                watchdog.disarm();
                long batchNanos = selectionEnd - updateStart;
                record.totalNanos += batchNanos;
                record.finalTuples = selected;

                LinkedHashMap<String, Long> residentRows = new LinkedHashMap<>();
                for (Map.Entry<String, EPCompiled> entry : residentQueries.entrySet()) {
                    residentRows.put(entry.getKey(), countQuery(faf, entry.getValue()));
                }

                LinkedHashMap<String, Object> perBatch = new LinkedHashMap<>();
                perBatch.put("b", batch.b);
                perBatch.put("ingest_ms", ms(ingestNanos));
                perBatch.put("update_ms", ms(updateEnd - updateStart));
                perBatch.put("compose_ms", ms(composeEnd - updateEnd));
                perBatch.put("selection_ms", ms(selectionEnd - composeEnd));
                perBatch.put("candidates", candidates.size());
                perBatch.put("count", selected.size());
                perBatch.put("resident_rows", residentRows);
                perBatch.put("heap_used_mb", heapUsedMb());
                record.perBatch.add(perBatch);
                sampleLiveHeap(record, cli, bi, data.batches.size(), perBatch);   // memory measurement only (--gc-samples), after the timed section

                System.err.printf(Locale.ROOT, "batch=%d timed_ms=%.3f count=%d%n",
                        batch.b, ms(batchNanos), selected.size());
                if (batchNanos > cli.wallNanos || record.totalNanos > cli.totalWallNanos) {
                    record.status = "dnf";
                    record.notes.append("Stopped after batch ").append(batch.b)
                            .append(" because timed wall was exceeded; batch_ms=")
                            .append(String.format(Locale.ROOT, "%.3f", ms(batchNanos)))
                            .append(", total_ms=")
                            .append(String.format(Locale.ROOT, "%.3f", ms(record.totalNanos)))
                            .append(".\n");
                    break;
                }
            }
        } finally {
            cleanup(runtime, deployment);
        }
        return record;
    }

    private static RunRecord runNative(DataSet data, Plan plan, Cli cli) throws Exception {
        RunRecord record = new RunRecord("native");
        record.notes.append("Native arm deployed plan.native.module as-is. Batch rows are sent as RowEvent outside ")
                .append("the timed region; the timed region iterates the named match_recognize statement and runs Selection. ")
                .append("No create index EPL is emitted by this program.\n");

        Configuration cfg = new Configuration();
        cfg.getRuntime().getThreading().setInternalTimerEnabled(false);
        Watchdog watchdog = new Watchdog(record, cli.out);
        EPRuntime runtime = EPRuntimeProvider.getRuntime("esper-native-" + System.nanoTime(), cfg);
        runtime.initialize();
        EPDeployment deployment = null;
        try {
            EPCompiler compiler = EPCompilerProvider.getCompiler();
            deployment = deploy(runtime, cfg, compiler, plan.nativePlan.module);
            EPStatement statement = requireStatement(runtime, deployment, plan.nativePlan.statement);

            Cursor cursor = new Cursor();
            for (DataSet.Batch batch : data.batches) {
                List<DataSet.Row> batchRows = takeBatchRows(data.rows, cursor, batch);
                long ingestStart = System.nanoTime();
                for (DataSet.Row row : batchRows) {
                    runtime.getEventService().sendEventMap(row.rowEvent(), "RowEvent");
                }
                long ingestNanos = System.nanoTime() - ingestStart;

                watchdog.arm(Math.min(cli.wallNanos, cli.totalWallNanos - record.totalNanos));
                long iterateStart = System.nanoTime();
                List<long[]> candidates = collectTuples(statement.iterator(), plan.keyColumns);
                long iterateEnd = System.nanoTime();
                List<long[]> selected = Selection.select(candidates);
                long selectionEnd = System.nanoTime();

                watchdog.disarm();
                long batchNanos = selectionEnd - iterateStart;
                record.totalNanos += batchNanos;
                record.finalTuples = selected;

                LinkedHashMap<String, Object> perBatch = new LinkedHashMap<>();
                perBatch.put("b", batch.b);
                perBatch.put("ingest_ms", ms(ingestNanos));
                perBatch.put("iterate_ms", ms(iterateEnd - iterateStart));
                perBatch.put("selection_ms", ms(selectionEnd - iterateEnd));
                perBatch.put("candidates", candidates.size());
                perBatch.put("count", selected.size());
                perBatch.put("heap_used_mb", heapUsedMb());
                record.perBatch.add(perBatch);
                sampleLiveHeap(record, cli, record.perBatch.size() - 1, data.batches.size(), perBatch);   // memory measurement only (--gc-samples)

                System.err.printf(Locale.ROOT, "batch=%d timed_ms=%.3f count=%d%n",
                        batch.b, ms(batchNanos), selected.size());
                if (batchNanos > cli.wallNanos || record.totalNanos > cli.totalWallNanos) {
                    record.status = "dnf";
                    record.notes.append("Stopped after batch ").append(batch.b)
                            .append(" because timed wall was exceeded; batch_ms=")
                            .append(String.format(Locale.ROOT, "%.3f", ms(batchNanos)))
                            .append(", total_ms=")
                            .append(String.format(Locale.ROOT, "%.3f", ms(record.totalNanos)))
                            .append(".\n");
                    break;
                }
            }
        } finally {
            cleanup(runtime, deployment);
        }
        return record;
    }

    private static EPDeployment deploy(EPRuntime runtime, Configuration cfg, EPCompiler compiler, String module) throws Exception {
        CompilerArguments args = new CompilerArguments(cfg);
        args.getPath().add(runtime.getRuntimePath());
        EPCompiled compiled = compiler.compile(module, args);
        return runtime.getDeploymentService().deploy(compiled);
    }

    private static EPCompiled compileQuery(EPRuntime runtime, Configuration cfg, EPCompiler compiler, String epl) throws Exception {
        CompilerArguments args = new CompilerArguments(cfg);
        args.getPath().add(runtime.getRuntimePath());
        return compiler.compileQuery(epl, args);
    }

    private static EPStatement requireStatement(EPRuntime runtime, EPDeployment deployment, String name) {
        EPStatement statement = runtime.getDeploymentService().getStatement(deployment.getDeploymentId(), name);
        if (statement == null) {
            throw new IllegalArgumentException("statement not found after deploy: " + name);
        }
        return statement;
    }

    private static List<DataSet.Row> takeBatchRows(List<DataSet.Row> rows, Cursor cursor, DataSet.Batch batch) {
        List<DataSet.Row> out = new ArrayList<>();
        while (cursor.index < rows.size() && rows.get(cursor.index).id <= batch.hi) {
            DataSet.Row row = rows.get(cursor.index++);
            if (row.id > batch.lo) {
                out.add(row);
            }
        }
        return out;
    }

    private static long minTs(List<DataSet.Row> rows) {
        long min = Long.MAX_VALUE;
        for (DataSet.Row row : rows) {
            min = Math.min(min, row.ts);
        }
        return min;
    }

    private static List<long[]> collectTuples(Iterator<EventBean> iterator, List<String> keyColumns) {
        List<long[]> tuples = new ArrayList<>();
        while (iterator.hasNext()) {
            tuples.add(tuple(iterator.next(), keyColumns));
        }
        return tuples;
    }

    private static long[] tuple(EventBean event, List<String> keyColumns) {
        long[] tuple = new long[keyColumns.size()];
        for (int i = 0; i < keyColumns.size(); i++) {
            Object value = event.get(keyColumns.get(i));
            if (!(value instanceof Number n)) {
                throw new IllegalStateException("key column " + keyColumns.get(i) + " is not numeric: " + value);
            }
            tuple[i] = n.longValue();
        }
        return tuple;
    }

    private static long countQuery(EPFireAndForgetService faf, EPCompiled query) {
        EPFireAndForgetQueryResult result = faf.executeQuery(query);
        Iterator<EventBean> iterator = result.iterator();
        if (!iterator.hasNext()) {
            return 0L;
        }
        Object value = iterator.next().get("c");
        if (!(value instanceof Number n)) {
            throw new IllegalStateException("count query did not return numeric c: " + value);
        }
        return n.longValue();
    }

    private static void cleanup(EPRuntime runtime, EPDeployment deployment) {
        if (deployment != null) {
            try {
                runtime.getDeploymentService().undeploy(deployment.getDeploymentId());
            } catch (Exception ignored) {
            }
        }
        runtime.destroy();
    }

    private static void writeRecord(Path out, RunRecord record) throws IOException {
        Path parent = out.getParent();
        if (parent != null) {
            Files.createDirectories(parent);
        }
        Json.mapper().writeValue(out.toFile(), record.toJson());
    }

    private static double ms(long nanos) {
        return nanos / 1_000_000.0;
    }

    private static long secondsToNanos(double seconds) {
        if (!(seconds > 0.0) || Double.isNaN(seconds) || Double.isInfinite(seconds)) {
            throw new IllegalArgumentException("wall values must be positive seconds");
        }
        return Math.round(seconds * Duration.ofSeconds(1).toNanos());
    }

    private static String compact(Throwable t) {
        StringBuilder sb = new StringBuilder();
        Throwable cur = t;
        int depth = 0;
        while (cur != null && depth < 8) {
            if (depth > 0) {
                sb.append(" | caused by: ");
            }
            sb.append(cur.getClass().getName());
            if (cur.getMessage() != null) {
                sb.append(": ").append(cur.getMessage());
            }
            cur = cur.getCause();
            depth++;
        }
        return sb.toString();
    }

    private static final class CandidateCollector implements com.espertech.esper.runtime.client.UpdateListener {
        private final List<String> keyColumns;
        private final List<long[]> candidates = new ArrayList<>();

        CandidateCollector(List<String> keyColumns) {
            this.keyColumns = List.copyOf(keyColumns);
        }

        @Override
        public synchronized void update(EventBean[] newEvents, EventBean[] oldEvents, EPStatement statement, EPRuntime runtime) {
            if (newEvents == null) {
                return;
            }
            for (EventBean event : newEvents) {
                candidates.add(tuple(event, keyColumns));
            }
        }

        synchronized void clear() {
            candidates.clear();
        }

        synchronized List<long[]> snapshot() {
            List<long[]> out = new ArrayList<>(candidates.size());
            for (long[] tuple : candidates) {
                out.add(tuple.clone());
            }
            return out;
        }
    }

    private static final class Cursor {
        int index;
    }

    private static final class Cli {
        final Path data;
        final Path plan;
        final String arm;
        final Path out;
        final long wallNanos;
        final long totalWallNanos;
        boolean finalGc;   // --final-gc 1: forced collections + live heap after the timed section (memory measurement)
        int gcSamples;     // --gc-samples S: forced collections at ~S batch boundaries (memory measurement)

        private Cli(Path data, Path plan, String arm, Path out, long wallNanos, long totalWallNanos) {
            this.data = data;
            this.plan = plan;
            this.arm = arm;
            this.out = out;
            this.wallNanos = wallNanos;
            this.totalWallNanos = totalWallNanos;
        }

        static Cli parse(String[] args) {
            LinkedHashMap<String, String> values = new LinkedHashMap<>();
            for (int i = 0; i < args.length; i++) {
                String key = args[i];
                if (!key.startsWith("--") || i + 1 >= args.length) {
                    throw new IllegalArgumentException("expected --key value, got: " + key);
                }
                values.put(key, args[++i]);
            }
            String arm = required(values, "--arm");
            if (!"eimer".equals(arm) && !"native".equals(arm) && !"none".equals(arm)) {
                throw new IllegalArgumentException("--arm must be eimer, native or none");
            }
            Cli cli = new Cli(
                    Path.of(required(values, "--data")),
                    Path.of(required(values, "--plan")),
                    arm,
                    Path.of(required(values, "--out")),
                    secondsToNanos(Double.parseDouble(required(values, "--wall-s"))),
                    secondsToNanos(Double.parseDouble(required(values, "--total-wall-s"))));
            cli.finalGc = "1".equals(values.getOrDefault("--final-gc", "0"));
            cli.gcSamples = Integer.parseInt(values.getOrDefault("--gc-samples", "0"));
            return cli;
        }

        private static String required(Map<String, String> values, String key) {
            String value = values.get(key);
            if (value == null || value.isBlank()) {
                throw new IllegalArgumentException("missing " + key);
            }
            return value;
        }
    }

    /** Heap in use after a batch (untimed): total minus free, no GC forced, so an upper bound on live data. */
    private static double heapUsedMb() {
        Runtime rt = Runtime.getRuntime();
        return (rt.totalMemory() - rt.freeMemory()) / (1024.0 * 1024.0);
    }

    private static final class RunRecord {
        final String arm;
        String status = "ok";
        long totalNanos;
        final List<LinkedHashMap<String, Object>> perBatch = new ArrayList<>();
        List<long[]> finalTuples = new ArrayList<>();
        final StringBuilder notes = new StringBuilder();
        String dataSha16;
        Double finalLiveHeapMb;   // set only with --final-gc 1 (memory measurement)
        double peakLiveHeapSampledMb = 0.0;   // --gc-samples: peak live heap over the forced collections at batch boundaries

        RunRecord(String arm) {
            this.arm = arm;
        }

        static RunRecord failed(String arm, Throwable t) {
            RunRecord record = new RunRecord(arm);
            record.status = "error";
            record.notes.append("Fatal exception: ").append(compact(t)).append('\n');
            return record;
        }

        LinkedHashMap<String, Object> toJson() {
            LinkedHashMap<String, Object> root = new LinkedHashMap<>();
            root.put("arm", arm);
            root.put("status", status);
            if (finalLiveHeapMb != null) {
                root.put("final_live_heap_mb", Math.round(finalLiveHeapMb * 10.0) / 10.0);
            }
            if (peakLiveHeapSampledMb > 0.0) {
                root.put("peak_live_heap_sampled_mb", Math.round(peakLiveHeapSampledMb * 10.0) / 10.0);
            }
            root.put("total_ms", ms(totalNanos));
            root.put("per_batch", perBatch);
            root.put("final_count", finalTuples.size());
            root.put("final_tuples", tupleLists(finalTuples));
            root.put("esper_version", esperVersion());
            root.put("jvm", jvmDescription());
            root.put("data_sha256_16", dataSha16);
            root.put("notes", notes.toString());
            return root;
        }

        private static List<List<Long>> tupleLists(List<long[]> tuples) {
            List<List<Long>> out = new ArrayList<>(tuples.size());
            for (long[] tuple : tuples) {
                List<Long> row = new ArrayList<>(tuple.length);
                for (long value : tuple) {
                    row.add(value);
                }
                out.add(row);
            }
            return out;
        }

        private static String jvmDescription() {
            return "java.version=" + System.getProperty("java.version")
                    + " input_arguments=" + ManagementFactory.getRuntimeMXBean().getInputArguments();
        }
    }
}
