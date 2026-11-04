package meridian.metrics;

// Hidden acceptance: R17 and R18.

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

class AcceptanceTest {
    @Test
    void parsesStatsLinesInOrder() {
        Map<String, String> fields = Stats.parse("STATS node=n1 keys=2 puts=3 lat=1,0,0,0,0,0,0,0");
        assertEquals(List.of("node", "keys", "puts", "lat"), List.copyOf(fields.keySet()));
        assertEquals("n1", fields.get("node"));
        assertEquals("1,0,0,0,0,0,0,0", fields.get("lat"));
        assertEquals(Map.of("router", "r1"), Stats.parse("STATS router=r1"));
    }

    @Test
    void rejectsMalformedStatsLines() {
        for (String line : List.of("STAT x=1", "STATS x", "STATS =1", "STATS a=1 a=2", "", "VALUE 1 x")) {
            assertThrows(IllegalArgumentException.class, () -> Stats.parse(line), line);
        }
    }

    @Test
    void pnCountersMergeLikeCrdts() {
        PNCounter a = new PNCounter();
        a.increment("n1", 5);
        a.decrement("n1", 2);
        PNCounter b = new PNCounter();
        b.increment("n1", 3);
        b.increment("n2", 4);
        b.decrement("n2", 1);
        PNCounter ab = new PNCounter();
        ab.merge(a);
        ab.merge(b);
        PNCounter ba = new PNCounter();
        ba.merge(b);
        ba.merge(a);
        assertEquals(5 - 2 + 4 - 1, ab.value());
        assertEquals(ab.value(), ba.value());
        ab.merge(a);
        ab.merge(ab);
        assertEquals(6, ab.value());
        assertThrows(IllegalArgumentException.class, () -> a.increment("n1", -1));
        assertThrows(IllegalArgumentException.class, () -> a.decrement("n1", -1));
    }

    @Test
    void histogramPercentilesUseNearestRank() {
        Histogram h = Histogram.fromCounts(new long[] {1, 1, 1, 1, 1, 1, 1, 1});
        assertEquals(8, h.total());
        assertEquals(100, h.percentile(12.5));
        assertEquals(1000, h.percentile(50));
        assertEquals(10000, h.percentile(87.5));
        assertEquals(Long.MAX_VALUE, h.percentile(100));
        Histogram other = Histogram.fromCounts(new long[] {8, 0, 0, 0, 0, 0, 0, 0});
        h.merge(other);
        assertEquals(16, h.total());
        assertEquals(100, h.percentile(50));
        assertEquals(1000, h.percentile(70)); // rank 12 of 9,1,1,1,...: the fourth bucket
        assertThrows(IllegalStateException.class, () -> new Histogram().percentile(50));
        assertThrows(IllegalArgumentException.class, () -> Histogram.fromCounts(new long[] {1, 2}));
        assertThrows(IllegalArgumentException.class,
                () -> Histogram.fromCounts(new long[] {1, 1, 1, 1, 1, 1, 1, -1}));
    }
}
