// Line protocol for offline verification against the actual Trama candidate classes.
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.util.Map;
import kotlinx.serialization.json.Json;
import kotlinx.serialization.json.JsonObject;
import run.trama.saga.JsonElementUtilsKt;
import run.trama.saga.MustacheTemplateRenderer;
import run.trama.saga.TemplateEscaping;
import run.trama.saga.TemplateString;

class TramaTypedJsonOracle {
    @SuppressWarnings("unchecked")
    public static void main(String[] args) throws Exception {
        var renderer = new MustacheTemplateRenderer();
        var reader = new BufferedReader(new InputStreamReader(System.in));
        String line;
        while ((line = reader.readLine()) != null) {
            try {
                var request = (JsonObject) Json.Default.parseToJsonElement(line);
                var context = (Map<String, Object>) JsonElementUtilsKt.toAny(request.get("context"));
                if (request.containsKey("expression")) {
                    var result = run.trama.saga.workflow.JsonLogicEvaluator.INSTANCE.evaluateBool(
                        request.get("expression"), context
                    );
                    System.out.println("{\"condition\":" + result + "}");
                    System.out.flush();
                    continue;
                }
                var source = Json.Default.decodeFromJsonElement(
                    run.trama.saga.TemplateStringSerializer.INSTANCE, request.get("body")
                );
                var result = renderer.renderTypedJson(source, context);
                System.out.println("{\"body\":" + result + "}");
            } catch (Exception e) {
                // Errors are protocol data; never print a Java stack trace on stdout.
                var message = kotlinx.serialization.json.JsonElementKt.JsonPrimitive(e.toString());
                System.out.println("{\"error\":" + message.toString() + "}");
            }
            System.out.flush();
        }
    }
}
