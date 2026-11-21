package eimer.esper;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/** Per-start selection of the compose candidate rows. */
public final class Selection {
    private Selection() {
    }

    public static List<long[]> select(List<long[]> candidates) {
        Map<Long, long[]> bestByFirst = new HashMap<>();
        for (long[] candidate : candidates) {
            if (candidate.length == 0) {
                throw new IllegalArgumentException("candidate tuple is empty");
            }
            long[] previous = bestByFirst.get(candidate[0]);
            if (previous == null || compareTail(candidate, previous) > 0) {
                bestByFirst.put(candidate[0], Arrays.copyOf(candidate, candidate.length));
            }
        }
        List<long[]> selected = new ArrayList<>(bestByFirst.values());
        selected.sort((a, b) -> Long.compare(a[0], b[0]));
        return selected;
    }

    private static int compareTail(long[] a, long[] b) {
        if (a.length != b.length) {
            throw new IllegalArgumentException("candidate tuple widths differ: " + a.length + " vs " + b.length);
        }
        for (int i = 1; i < a.length; i++) {
            int cmp = Long.compare(a[i], b[i]);
            if (cmp != 0) {
                return cmp;
            }
        }
        return 0;
    }
}
