package eimer.esper;

import com.fasterxml.jackson.core.type.TypeReference;

import java.io.BufferedReader;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/** Reads the data.csv event table and splits it into batches. */
final class DataSet {
    final List<Row> rows;
    final List<Batch> batches;
    final String timestampColumn;

    private DataSet(List<Row> rows, List<Batch> batches, String timestampColumn) {
        this.rows = rows;
        this.batches = batches;
        this.timestampColumn = timestampColumn;
    }

    static String dataSha16(Path dir) throws IOException {
        try {
            java.security.MessageDigest md = java.security.MessageDigest.getInstance("SHA-256");
            byte[] digest = md.digest(Files.readAllBytes(dir.resolve("data.csv")));
            StringBuilder sb = new StringBuilder();
            for (int i = 0; i < 8; i++) {
                sb.append(String.format(Locale.ROOT, "%02x", digest[i]));
            }
            return sb.toString();
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    static DataSet read(Path dir, Plan plan) throws IOException {
        String tsColumn = plan.timestampColumn();
        return new DataSet(
                readRows(dir.resolve("data.csv"), plan.columns, tsColumn),
                readBatches(dir.resolve("batches.json")),
                tsColumn);
    }

    private static List<Row> readRows(Path csv, LinkedHashMap<String, String> columns, String tsColumn) throws IOException {
        List<Row> rows = new ArrayList<>();
        try (BufferedReader reader = Files.newBufferedReader(csv)) {
            String headerLine = reader.readLine();
            if (headerLine == null) {
                throw new IOException("empty CSV: " + csv);
            }
            List<String> header = splitCsv(headerLine);
            List<String> eventColumns = new ArrayList<>(columns.keySet());
            if (header.size() != eventColumns.size()) {
                throw new IOException("CSV header length " + header.size() + " does not match plan columns "
                        + eventColumns.size() + " in " + csv);
            }
            List<String> mappedColumns = mapHeader(header, eventColumns, tsColumn);
            String line;
            long lineNo = 1L;
            while ((line = reader.readLine()) != null) {
                lineNo++;
                if (line.isBlank()) {
                    continue;
                }
                List<String> fields = splitCsv(line);
                if (fields.size() != mappedColumns.size()) {
                    throw new IOException("CSV line " + lineNo + " has " + fields.size()
                            + " fields, expected " + mappedColumns.size());
                }
                LinkedHashMap<String, Object> values = new LinkedHashMap<>();
                for (int i = 0; i < mappedColumns.size(); i++) {
                    String column = mappedColumns.get(i);
                    values.put(column, parseValue(fields.get(i), columns.get(column), column, lineNo));
                }
                rows.add(new Row(asLong(values.get("id"), "id", lineNo), asLong(values.get(tsColumn), tsColumn, lineNo), values));
            }
        }
        rows.sort(Comparator.comparingLong(row -> row.id));
        return rows;
    }

    private static List<String> mapHeader(List<String> header, List<String> eventColumns, String tsColumn) throws IOException {
        List<String> out = new ArrayList<>(header.size());
        for (int i = 0; i < header.size(); i++) {
            String actual = header.get(i);
            String expected = eventColumns.get(i);
            if (actual.equals(expected)) {
                out.add(expected);
            } else if (expected.equals(tsColumn) && isTimestampAlias(actual, tsColumn)) {
                out.add(expected);
            } else {
                throw new IOException("CSV header column " + (i + 1) + " is " + actual
                        + ", expected " + expected + timestampAliasNote(expected));
            }
        }
        return out;
    }

    private static boolean isTimestampAlias(String actual, String expected) {
        return ("ts".equals(expected) && "ts_ms".equals(actual)) || ("ts_ms".equals(expected) && "ts".equals(actual));
    }

    private static String timestampAliasNote(String expected) {
        if ("ts".equals(expected)) {
            return " (ts_ms is also accepted)";
        }
        if ("ts_ms".equals(expected)) {
            return " (ts is also accepted)";
        }
        return "";
    }

    private static Object parseValue(String raw, String type, String column, long lineNo) throws IOException {
        if (type == null) {
            throw new IOException("no type for column " + column);
        }
        try {
            return switch (type.toLowerCase(Locale.ROOT)) {
                case "long", "bigint", "int", "integer" -> Long.parseLong(raw);
                case "double", "float", "real" -> Double.parseDouble(raw);
                case "string", "varchar" -> raw;
                default -> throw new IOException("unsupported type " + type + " for column " + column);
            };
        } catch (NumberFormatException ex) {
            throw new IOException("cannot parse " + column + " on CSV line " + lineNo
                    + " as " + type + ": " + raw, ex);
        }
    }

    private static long asLong(Object value, String column, long lineNo) throws IOException {
        if (value instanceof Number n) {
            return n.longValue();
        }
        throw new IOException("column " + column + " on CSV line " + lineNo + " is not numeric");
    }

    private static List<Batch> readBatches(Path batchesJson) throws IOException {
        List<Batch> batches = Json.mapper().readValue(Files.readString(batchesJson), new TypeReference<>() {
        });
        if (batches.isEmpty()) {
            throw new IOException("no batches in " + batchesJson);
        }
        return batches;
    }

    private static List<String> splitCsv(String line) throws IOException {
        List<String> out = new ArrayList<>();
        StringBuilder field = new StringBuilder();
        boolean quoted = false;
        for (int i = 0; i < line.length(); i++) {
            char c = line.charAt(i);
            if (quoted) {
                if (c == '"') {
                    if (i + 1 < line.length() && line.charAt(i + 1) == '"') {
                        field.append('"');
                        i++;
                    } else {
                        quoted = false;
                    }
                } else {
                    field.append(c);
                }
            } else if (c == ',') {
                out.add(field.toString());
                field.setLength(0);
            } else if (c == '"') {
                if (field.length() != 0) {
                    throw new IOException("quote inside unquoted CSV field: " + line);
                }
                quoted = true;
            } else {
                field.append(c);
            }
        }
        if (quoted) {
            throw new IOException("unterminated quoted CSV field: " + line);
        }
        out.add(field.toString());
        return out;
    }

    static final class Row {
        final long id;
        final long ts;
        private final LinkedHashMap<String, Object> values;

        Row(long id, long ts, LinkedHashMap<String, Object> values) {
            this.id = id;
            this.ts = ts;
            this.values = values;
        }

        Map<String, Object> rowEvent() {
            return new LinkedHashMap<>(values);
        }

        Map<String, Object> driveEvent(long batchMinTs) {
            LinkedHashMap<String, Object> event = new LinkedHashMap<>(values);
            event.put("batch_min_ts", batchMinTs);
            return event;
        }
    }

    static final class Batch {
        public int b;
        public long lo;
        public long hi;
    }
}
