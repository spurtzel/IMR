package eimer.esper;

import com.fasterxml.jackson.annotation.JsonProperty;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;

/** The plan.json content: variables, the eimer view plan and the native statement. */
final class Plan {
    public List<String> variables = new ArrayList<>();
    @JsonProperty("key_columns")
    public List<String> keyColumns = new ArrayList<>();
    public LinkedHashMap<String, String> columns = new LinkedHashMap<>();
    @JsonProperty("w_ms")
    public long wMs;
    public EimerPlan eimer;
    @JsonProperty("native")
    public NativePlan nativePlan;

    void validate(String arm) {
        require(!variables.isEmpty(), "plan.variables is empty");
        require(!keyColumns.isEmpty(), "plan.key_columns is empty");
        require(keyColumns.size() == variables.size(), "plan.key_columns must have one key per variable");
        require(columns.containsKey("id"), "plan.columns must contain id");
        require(columns.containsKey(timestampColumn()), "plan.columns must contain ts or ts_ms");
        if ("eimer".equals(arm)) {
            require(eimer != null, "plan.eimer is missing");
            require(nonBlank(eimer.module), "plan.eimer.module is missing");
            require(nonBlank(eimer.composeStatement), "plan.eimer.compose_statement is missing");
            require(eimer.views != null, "plan.eimer.views is missing");
            for (ViewPlan view : eimer.views) {
                require(nonBlank(view.name), "plan.eimer.views[].name is missing");
                require(nonBlank(view.updateStatement), "plan.eimer.views[].update_statement is missing");
            }
        } else if ("native".equals(arm)) {
            require(nativePlan != null, "plan.native is missing");
            require(nonBlank(nativePlan.module), "plan.native.module is missing");
            require(nonBlank(nativePlan.statement), "plan.native.statement is missing");
        } else {
            throw new IllegalArgumentException("unknown arm: " + arm);
        }
    }

    String timestampColumn() {
        return columns.containsKey("ts") ? "ts" : "ts_ms";
    }

    private static boolean nonBlank(String s) {
        return s != null && !s.isBlank();
    }

    private static void require(boolean ok, String message) {
        if (!ok) {
            throw new IllegalArgumentException(message);
        }
    }

    static final class EimerPlan {
        public String module;
        public List<ViewPlan> views = new ArrayList<>();
        @JsonProperty("compose_statement")
        public String composeStatement;
    }

    static final class ViewPlan {
        public String name;
        public List<String> vars = new ArrayList<>();
        @JsonProperty("update_statement")
        public String updateStatement;
    }

    static final class NativePlan {
        public String module;
        public String statement;
    }
}
