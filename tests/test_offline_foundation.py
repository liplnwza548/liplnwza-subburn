"""Offline regression tests; all approval data is synthetic and temporary."""
import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK = ROOT / 'Subtitle-Burn-Colab.ipynb'


def notebook_functions(name):
    notebook = json.loads(NOTEBOOK.read_text(encoding='utf-8'))
    result = []
    for cell in notebook['cells']:
        if cell['cell_type'] != 'code':
            continue
        source = ''.join(cell['source'])
        try:
            tree = ast.parse(source)
        except SyntaxError:  # Other Colab cells can contain IPython syntax.
            continue
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == name:
                result.append(node)
    return result


def load_function(node, namespace):
    tree = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
    exec(compile(tree, str(NOTEBOOK), 'exec'), namespace)
    return namespace[node.name]


class NotebookApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.approval = Path(self.temp.name) / 'approval.json'
        self.nodes = notebook_functions('_require_approval')
        self.assertEqual(len(self.nodes), 3, 'Review all notebook burn approval gates')

    def helpers(self):
        for node in self.nodes:
            yield load_function(node, {'Path': lambda _: self.approval, 'json': json})

    def test_burn_cells_stop_before_subprocess_without_approval(self):
        notebook = json.loads(NOTEBOOK.read_text(encoding='utf-8'))
        cells = [''.join(c['source']) for c in notebook['cells']
                 if c['cell_type'] == 'code' and 'def _require_approval(' in ''.join(c['source'])]
        self.assertEqual(len(cells), 3)
        real_path = Path
        def isolated_path(value):
            if str(value) == '/content/subburn_work/approval.json':
                return self.approval
            return real_path(value)
        for index, source in enumerate(cells):
            with self.subTest(cell=index), patch('pathlib.Path', side_effect=isolated_path), \
                    patch('subprocess.run') as run:
                with self.assertRaisesRegex(SystemExit, 'approval.json'):
                    exec(compile(source, str(NOTEBOOK), 'exec'), {})
                run.assert_not_called()

    def test_missing_approval_refused(self):
        for gate in self.helpers():
            with self.subTest(line=gate.__code__.co_firstlineno):
                with self.assertRaises(SystemExit):
                    gate('paper-chips')

    def test_invalid_or_wrong_style_refused(self):
        for payload in ('broken json', json.dumps({'approved': False, 'style': 'paper-chips'}),
                        json.dumps({'approved': True, 'style': 'unknown'}),
                        json.dumps({'approved': True, 'style': 'viralpop'})):
            self.approval.write_text(payload, encoding='utf-8')
            for gate in self.helpers():
                with self.subTest(payload=payload, line=gate.__code__.co_firstlineno):
                    with self.assertRaises(SystemExit):
                        gate('paper-chips')

    def test_matching_synthetic_approval_accepted(self):
        self.approval.write_text(json.dumps({'approved': True, 'style': 'paper-chips'}), encoding='utf-8')
        for gate in self.helpers():
            self.assertEqual(gate('paper-chips')['style'], 'paper-chips')

    def test_changed_script_rejected(self):
        nodes = notebook_functions('_check_approved_script')
        self.assertGreaterEqual(len(nodes), 1)
        script = Path(self.temp.name) / 'synthetic-script.txt'
        script.write_text('sample', encoding='utf-8')
        digest = hashlib.sha256(script.read_bytes()).hexdigest()
        self.approval.write_text(json.dumps({'approved': True, 'script_sha256': {'01': digest}}), encoding='utf-8')
        for node in nodes:
            gate = load_function(node, {'Path': lambda _: self.approval, 'json': json,
                                       '_sha256': lambda path: hashlib.sha256(path.read_bytes()).hexdigest()})
            gate('01', script)
            script.write_text('changed', encoding='utf-8')
            with self.assertRaises(SystemExit):
                gate('01', script)
            script.write_text('sample', encoding='utf-8')


class StandaloneStyleTests(unittest.TestCase):
    def test_unknown_word_is_not_guessed_when_chunking(self):
        path = ROOT / 'subtitle/styles/burn_styles.py'
        spec = importlib.util.spec_from_file_location('burn_styles', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with self.assertRaisesRegex(ValueError, 'no syllable count'):
            module.chunk_phrases([{'words': [{'word': 'คำสมมติที่ไม่มีในตาราง'}]}])


if __name__ == '__main__':
    unittest.main()
