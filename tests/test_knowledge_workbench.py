import tempfile
import unittest
from tests.test_thinking_params import run_in_app


class KnowledgeWorkbenchTests(unittest.TestCase):
    def probe(self,name):
        with tempfile.TemporaryDirectory() as root:
            result=run_in_app('import asyncio\nfrom tests import knowledge_workbench_probe as p\n'+f'print(json.dumps(asyncio.run(p.{name}(bot,{root!r}))))')
            self.assertTrue(all(result.values()))
    def test_document_crud_keeps_runtime_skill_state_and_memory_semantics(self):self.probe('documents')
    def test_task_source_search_reads_archived_history_without_switching(self):self.probe('source_search')
