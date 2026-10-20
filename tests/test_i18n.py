"""Tests for i18n team-name translation (P6)."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import core.i18n as i18n
from core.i18n import to_cn, warm_translations


class TestI18N(unittest.TestCase):
    def setUp(self):
        # 用临时缓存文件，避免污染仓库 references/
        self._tmp = tempfile.TemporaryDirectory()
        self._cache_file = Path(self._tmp.name) / "team_translations.json"
        self._orig_file = i18n.TRANSLATION_CACHE_FILE
        i18n.TRANSLATION_CACHE_FILE = self._cache_file
        i18n._CACHE = None
        # 竞彩官方队名表同样指向临时文件：单测不该依赖仓库里那张表的真实内容
        # （否则改表就会连带改单测期望）。另有一个用真实表断言优先级的用例。
        self._official_file = Path(self._tmp.name) / "team_cn_official.json"
        self._orig_official = i18n.OFFICIAL_NAMES_FILE
        i18n.OFFICIAL_NAMES_FILE = self._official_file
        i18n._OFFICIAL = None

    def tearDown(self):
        i18n.TRANSLATION_CACHE_FILE = self._orig_file
        i18n._CACHE = None
        i18n.OFFICIAL_NAMES_FILE = self._orig_official
        i18n._OFFICIAL = None
        self._tmp.cleanup()

    def test_country_still_mapped(self):
        # 国名走 COUNTRY_CN，不依赖 LLM
        self.assertEqual(to_cn("England"), "英格兰")

    def test_unknown_without_cache_returns_original(self):
        self.assertEqual(to_cn("SomeUnknownFC"), "SomeUnknownFC")

    def test_warm_translates_and_persists(self):
        fake = {"Arsenal": "阿森纳", "Test FC": "测试队"}
        with patch("ai.llm_client.generate", return_value={"translations": fake}):
            warm_translations(["Arsenal", "Test FC"])
        self.assertEqual(to_cn("Arsenal"), "阿森纳")
        self.assertEqual(to_cn("Test FC"), "测试队")
        # 已持久化到缓存文件
        self.assertTrue(self._cache_file.exists())
        import json
        data = json.loads(self._cache_file.read_text(encoding="utf-8"))
        self.assertEqual(data["Arsenal"], "阿森纳")

    def test_official_names_take_priority_over_llm_cache(self):
        """竞彩官方队名表**优先于** LLM 翻译缓存（路线图《建议 1》队名链）。

        竞彩↔模型按中文名 join，中文名必须与竞彩官方叫法逐字相等；
        LLM 给的是球迷通用叫法（曼城/曼联/热刺/巴黎圣日耳曼），必然 join miss。
        """
        import json
        self._official_file.write_text(
            json.dumps({"Manchester City": "曼彻斯特城", "_meta": "ignored"}),
            encoding="utf-8",
        )
        i18n._OFFICIAL = None
        with patch("ai.llm_client.generate", return_value={"translations": {"Manchester City": "曼城"}}):
            warm_translations(["Manchester City"])
        # LLM 缓存里写的是「曼城」，但 to_cn 必须返回官方表的「曼彻斯特城」
        self.assertEqual(to_cn("Manchester City"), "曼彻斯特城")
        # 元信息键（_ 开头）不进查询
        self.assertEqual(to_cn("_meta"), "_meta")

    def test_warm_skips_already_cached(self):
        # 第一次预热写入
        with patch("ai.llm_client.generate", return_value={"translations": {"Chelsea": "切尔西"}}):
            warm_translations(["Chelsea"])
        # 仅传已缓存项时不应再调用 LLM
        with patch("ai.llm_client.generate") as mock_gen:
            warm_translations(["Chelsea"])
        mock_gen.assert_not_called()
        self.assertEqual(to_cn("Chelsea"), "切尔西")


if __name__ == "__main__":
    unittest.main()
