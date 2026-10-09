// Offline contract probe using Trama's compiled production renderer.
import java.util.List;
import java.util.Map;
import run.trama.saga.MustacheTemplateRenderer;
import run.trama.saga.TemplateEscaping;
import run.trama.saga.TemplateString;

class TramaTemplateOracle {
    public static void main(String[] args) throws Exception {
        var context = Map.of("nodes", Map.of("join", Map.of("response", Map.of("body", Map.of(
            "branches", List.of(
                Map.of("result", Map.of("output", Map.of("price", 111111))),
                Map.of("result", Map.of("output", Map.of("price", 2222)))
            )
        )))));
        for (String path : List.of(
            "nodes.join.response.body.branches.0.result.output.price",
            "nodes.join.response.body.branches[0].result.output.price",
            "nodes.join.response.body.branches"
        )) {
            var output = new MustacheTemplateRenderer().render(
                new TemplateString("{{ " + path + " }}"), context, TemplateEscaping.JSON_STRING
            );
            System.out.println(path + " => " + output);
        }
    }
}
