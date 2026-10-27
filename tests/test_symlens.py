import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ["SYMLENS_CACHE_DIR"] = tempfile.mkdtemp()      # never touch the user's cache from tests
import symlens as S  # noqa: E402

JAVA = textwrap.dedent('''\
    package a.b;

    import java.util.*;

    /** Javadoc with { braces } that must not count. */
    @Service
    @Transactional(propagation = Propagation.REQUIRES_NEW, readOnly = true)
    public class OrderService extends Base<Order> implements Api {
        private static final String Q = "select { from } where \\" x";
        private final char brace = '{';
        private final Map<String, List<Integer>> cache = new HashMap<>();
        private final Runnable job = () -> {
            System.out.println("}");
        };
        private final Comparator<Order> cmp = new Comparator<Order>() {
            @Override
            public int compare(Order a, Order b) { return 0; }
        };
        static {
            init();
        }

        public OrderService(Repo repo) {
            super(repo);
        }

        @Transactional(timeout = 5)
        public <T extends Comparable<T>> List<T> closePositionById(String id, BigDecimal q) throws IOException {
            String block = """
                text block with } and { inside
                """;
            if (q == null) { return null; }
            // comment with }
            return list(id, q);
        }

        abstract void later(int x);

        public record Point(int x, int y) {
            public Point {
                check(x);
            }
            int sum() { return x + y; }
        }

        enum Mode {
            ISOLATED("i") { int lev() { return 1; } },
            CROSS("c");
            private final String code;
            Mode(String code) { this.code = code; }
            String code() { return code; }
        }

        public @interface Marker {
            String value() default "x";
        }

        void save(Record record) {
            store(record);
        }
    }
    ''')

PY = textwrap.dedent('''\
    import os

    LIMIT = 10


    def helper(a, b=2) -> int:
        def inner():
            return a
        return inner()


    class Box(Base, metaclass=Meta):
        size: int = 3

        @property
        def area(self):
            return helper(self.size)

        async def load(self, path):
            return path
    ''')

TS = textwrap.dedent('''\
    import x from "y";

    export const API = "/api";

    export interface Props {
        id: string;
        onClose: () => void;
    }

    type Status = "a" | "b";

    export function Widget({ id, open = false }: Props): JSX.Element {
        const local = compute(id);
        const handler = () => local;
        return render(local, handler);
    }

    export const useThing = (n: number) => {
        return n * 2;
    };

    export class Store extends Base {
        size = 0;
        constructor(x: number) {
            this.size = x;
        }
        async load(key: string): Promise<void> {
            await fetch(key);
        }
        get total() { return this.size; }
    }
    ''')


def names(syms):
    return {s.qual: (s.kind, s.start, s.end) for s in syms}


class JavaOutline(unittest.TestCase):
    def setUp(self):
        self.syms = names(S.outline_java(JAVA))

    def test_types_and_members(self):
        n = self.syms
        self.assertEqual(n["OrderService"][:2], ("class", 6))
        self.assertEqual(n["OrderService"][2], JAVA.count("\n"))
        self.assertEqual(n["OrderService.OrderService"][0], "constructor")
        k, start, end = n["OrderService.closePositionById"]
        self.assertEqual((k, start), ("method", 27))       # the annotation line, not the Javadoc
        self.assertEqual(JAVA.splitlines()[end - 1].strip(), "}")
        self.assertEqual(end, 35)
        self.assertEqual(n["OrderService.later"][0], "method")
        self.assertEqual(n["OrderService.Point"][0], "record")
        self.assertEqual(n["OrderService.Point.sum"][0], "method")
        self.assertEqual(n["OrderService.Mode"][0], "enum")
        self.assertEqual(n["OrderService.Mode.Mode"][0], "constructor")
        self.assertEqual(n["OrderService.Mode.code"][0], "method")
        self.assertEqual(n["OrderService.Marker"][0], "annotation")
        self.assertEqual(n["OrderService.Marker.value"][0], "method")
        self.assertEqual(n["OrderService.save"][0], "method")

    def test_fields_and_no_false_methods(self):
        n = self.syms
        for f in ("Q", "brace", "cache", "job", "cmp"):
            self.assertEqual(n["OrderService." + f][0], "field", f)
        self.assertNotIn("OrderService.compare", n)       # method of an anonymous class is not a member
        self.assertNotIn("OrderService.lev", n)            # nor of an enum constant body
        self.assertNotIn("OrderService.Mode.lev", n)
        self.assertNotIn("OrderService.record", n)         # 'record' as a parameter name is not a type

    def test_signature(self):
        sig = [s for s in S.outline_java(JAVA) if s.qual == "OrderService.closePositionById"][0].sig
        self.assertEqual(sig, "public <T extends Comparable<T>> List<T> closePositionById(String id, BigDecimal q) "
                              "throws IOException")


class PythonOutline(unittest.TestCase):
    def test_outline(self):
        n = names(S.outline_python(PY))
        self.assertEqual(n["LIMIT"][0], "var")
        self.assertEqual(n["helper"], ("function", 6, 9))
        self.assertEqual(n["helper.inner"][0], "function")
        self.assertEqual(n["Box"][:2], ("class", 12))
        self.assertEqual(n["Box.size"][0], "var")
        self.assertEqual(n["Box.area"], ("method", 15, 17))   # decorator line included
        self.assertEqual(n["Box.load"][0], "method")

    def test_docstrings(self):
        src = 'def f(x):\n    """first line.\n    second line."""\n    return x\n'
        doc = {s.qual: s.doc for s in S.outline_python(src)}
        self.assertEqual(doc["f"], "first line.")


class TsOutline(unittest.TestCase):
    def test_outline(self):
        n = names(S.outline_ts(TS))
        self.assertEqual(n["API"][0], "var")
        self.assertEqual(n["Props"][0], "interface")
        self.assertEqual(n["Status"][0], "type")
        self.assertEqual(n["Widget"][0], "function")
        self.assertEqual(n["useThing"][0], "function")
        self.assertEqual(n["Store"][0], "class")
        self.assertEqual(n["Store.constructor"][0], "method")
        self.assertEqual(n["Store.load"][0], "method")
        self.assertEqual(n["Store.total"][0], "method")       # accessor: 'get' stripped, name kept
        _, start, end = n["Widget"]
        self.assertGreaterEqual(end - start, 3)               # destructured params don't truncate the body
        self.assertNotIn("Widget.local", n)                   # locals inside a function are not symbols
        self.assertNotIn("Widget.handler", n)


class Operations(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.dir, "src"))
        os.makedirs(os.path.join(self.dir, "copy"))
        for rel, text in (("src/OrderService.java", JAVA), ("src/box.py", PY), ("copy/box.py", PY)):
            with open(os.path.join(self.dir, rel), "w", encoding="utf-8") as fh:
                fh.write(text)
        with open(os.path.join(self.dir, ".symlens.json"), "w", encoding="utf-8") as fh:
            json.dump({"exclude": ["copy/**"]}, fh)

    def test_body_find_refs(self):
        body = S.op_body(self.dir, "src/OrderService.java", "closePositionById")
        self.assertIn("return list(id, q);", body)
        self.assertNotIn("abstract void later", body)
        found = S.op_find(self.dir, "area")
        self.assertIn("src/box.py:15-17 method Box.area", found)
        self.assertNotIn("copy/", found)                   # excluded by .symlens.json
        refs = S.op_refs(self.dir, "helper")
        self.assertIn("[Box.area]", refs)
        self.assertNotIn("  6 ", refs)                     # the definition line itself is not a reference
        self.assertIn("3 symbols", S.op_outline(self.dir, "src/box.py", depth=1))  # LIMIT, helper, Box

    def test_signature_at_docs(self):
        sig = S.op_body(self.dir, "src/box.py", "helper", signature=True)
        self.assertIn("def helper(a, b=2) -> int", sig)
        self.assertNotIn("return inner()", sig)            # signature mode omits the body
        self.assertIn("Box.area", S.op_at(self.dir, "src/box.py", 16))   # enclosing symbol of a line
        self.assertIn("symbols", S.op_outline(self.dir, "src/box.py", docs=True))
        refs = S.op_refs(self.dir, "self.size")            # dotted name matches only `self.size`
        self.assertIn("self.size", refs)

    def test_mcp_roundtrip(self):
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "symbol_body", "arguments": {"path": "src/box.py", "name": "Box.load"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "symbol_body", "arguments": {"path": "nope.py", "name": "x"}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
             "params": {"name": "enclosing_symbol", "arguments": {"path": "src/box.py", "line": 16}}},
        ]
        inp = "".join(json.dumps(m) + "\n" for m in msgs).encode("utf-8")
        out = subprocess.run([sys.executable, os.path.join(os.path.dirname(HERE), "symlens.py"), "--root",
                              self.dir, "serve"], input=inp, capture_output=True, timeout=60).stdout
        replies = [json.loads(x) for x in out.decode("utf-8").splitlines() if x.strip()]
        self.assertEqual([r["id"] for r in replies], [1, 2, 3, 4, 5])
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(len(replies[1]["result"]["tools"]), 5)
        self.assertIn("async def load", replies[2]["result"]["content"][0]["text"])
        self.assertTrue(replies[3]["result"]["isError"])
        self.assertIn("Box.area", replies[4]["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
