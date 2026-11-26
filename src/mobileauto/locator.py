"""分层按钮定位：L0 控件ID/稳定属性 → L1 控件文本 → L2 模板匹配 → L3 OCR → L4 比例坐标。

先快后慢、先稳后兜底：每一级找不到（或不可点击）才进入下一级，命中即返回。
所有层级统一返回 Hit，调用方只需关心坐标和命中层级。
"""

import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from mobileauto.paths import TEMPLATE_DIR

_BLOCK_WORDS = ("秒", "解锁", "后再", "再看", "提前", "完成", "互动", "任务", "福利", "第")
_SYSTEM_PREFIXES = ("com.android.systemui", "com.android.launcher", "com.miui.systemui", "com.android.inputmethod")


def normalize_text(s: Any) -> str:
    """统一文本：去空格、去 OCR 噪声、修正常见混淆字。"""
    if not s:
        return ""
    s = str(s).strip().replace(" ", "").replace("\u3000", "")
    s = re.sub(r"[×xX✕✖×·•\|丨\[\]【】()（）\d:：]+$", "", s)
    s = re.sub(r"^[×xX✕✖·•\|丨]+", "", s)
    s = s.replace("领収", "领取").replace("领职", "领取").replace("领敢", "领取")
    s = s.replace("奨", "奖").replace("奬", "奖").replace("勵", "励")
    return s


def text_match(recognized: Any, target: str, fuzzy: bool = True) -> bool:
    """精确优先的按钮文字匹配：允许 1 个字误差，但拒绝长句/倒计时。"""
    a = normalize_text(recognized)
    b = normalize_text(target)
    if not a or not b:
        return False
    core = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", a)
    if core == b:
        return True
    if any(word in a for word in _BLOCK_WORDS):
        return False
    if not fuzzy:
        return False
    if abs(len(core) - len(b)) > 1:
        return False
    if len(core) == len(b):
        return sum(1 for x, y in zip(core, b) if x != y) <= 1
    longer, shorter = (core, b) if len(core) > len(b) else (b, core)
    return any(longer[:i] + longer[i + 1:] == shorter for i in range(len(longer)))


@dataclass
class Hit:
    """一次定位结果。level 取值 L0~L4，便于日志确认实际走了哪一级。"""

    target: str
    x: int
    y: int
    level: str
    source: str
    label: str
    score: float = 1.0
    row: Optional[Dict[str, Any]] = None

    def describe(self) -> str:
        return (
            f"[{self.level}/{self.source}] 目标「{self.target}」"
            f"内容「{self.label}」@({self.x},{self.y}) score={self.score:.2f}"
        )


class LayeredLocator:
    """按 L0→L4 顺序查找目标。构造参数均可缺省，逐级按需启用。"""

    def __init__(
        self,
        targets: Sequence[str],
        *,
        target_ids: Optional[Dict[str, Sequence[str]]] = None,
        target_descs: Optional[Dict[str, Sequence[str]]] = None,
        target_roi: Optional[Dict[str, Dict[str, Tuple[float, float]]]] = None,
        target_ratio: Optional[Dict[str, Tuple[float, float]]] = None,
        template_dir: str = TEMPLATE_DIR,
        template_threshold: float = 0.85,
        clickable_only: bool = True,
        hide_system: bool = True,
        fuzzy: bool = True,
        log: Optional[Any] = None,
    ):
        self.targets = list(targets)
        self.target_ids: Dict[str, List[str]] = {
            k: list(v) for k, v in (target_ids or {}).items()
        }
        self.target_descs: Dict[str, List[str]] = {
            k: list(v) for k, v in (target_descs or {}).items()
        }
        for target in self.targets:
            self.target_descs.setdefault(target, [target])
        self.target_roi = dict(target_roi or {})
        self.target_ratio = dict(target_ratio or {})
        self.template_dir = template_dir
        self.template_threshold = template_threshold
        self.clickable_only = clickable_only
        self.hide_system = hide_system
        self.fuzzy = fuzzy
        self._log = log or (lambda message: None)
        self.last_click: Dict[Tuple[Any, int, int], float] = {}
        self._template_cache: Dict[str, List[Tuple[str, Any]]] = {}

    # ---------- 公共 API ----------
    def locate(
        self,
        target: str,
        *,
        rows: Optional[Sequence[Dict[str, Any]]] = None,
        screen: Any = None,
        ocr_items: Optional[Sequence[Tuple[str, float, float, float]]] = None,
        sw: int = 0,
        sh: int = 0,
        dedupe_window: float = 0.0,
    ) -> Optional[Hit]:
        """按 L0→L4 为一个目标定位。返回命中的 Hit，全部失败返回 None。"""
        row_list = list(rows or [])
        candidates: List[Hit] = []
        candidates.extend(self._by_attr(target, row_list))
        candidates.extend(self._by_text(target, row_list))
        candidates.extend(self._by_template(target, screen))
        candidates.extend(self._by_ocr(target, ocr_items or [], sw, sh))
        candidates.extend(self._by_ratio(target, sw, sh))
        if not candidates:
            return None

        candidates = [
            c for c in candidates
            if c.level == "L4" or self._in_roi(c.x, c.y, sw, sh, target)
        ]
        if not candidates:
            return None

        # 置信度优先；同级里控件命中优于视觉兜底
        candidates.sort(key=lambda c: (-c.score, c.level))
        hit = candidates[0]
        if dedupe_window > 0 and self._is_dupe(target, hit.x, hit.y, dedupe_window):
            self._log(f"· 跳过重复点击「{target}」@({hit.x},{hit.y})")
            return None
        self._remember(target, hit.x, hit.y)
        return hit

    def locate_success_close(
        self,
        *,
        rows: Optional[Sequence[Dict[str, Any]]] = None,
        sw: int = 0,
        sh: int = 0,
        dedupe_window: float = 0.0,
    ) -> Optional[Hit]:
        """关闭「领取成功」提示条：L0 找右上角关闭控件，否则 L4 比例坐标。"""
        candidates: List[Hit] = []
        for row in rows or []:
            label = " ".join(
                str(row.get(key, "") or "")
                for key in ("text", "desc", "resource_id", "class")
            )
            if re.search(r"(close|关闭|×|dismiss)", label, re.I) and self._is_top_right(row, sw, sh):
                candidates.append(
                    Hit("领取成功_X", row["cx"], row["cy"], "L0", "attr", label.strip(), 1.0, row)
                )
        ratio = self.target_ratio.get("领取成功_X")
        if ratio and sw and sh:
            candidates.append(
                Hit(
                    "领取成功_X",
                    int(sw * ratio[0]),
                    int(sh * ratio[1]),
                    "L4",
                    "ratio",
                    "右上角固定坐标",
                    0.8,
                )
            )
        if not candidates:
            return None
        candidates.sort(key=lambda c: (c.level, -c.score))
        hit = candidates[0]
        if dedupe_window > 0 and self._is_dupe(hit.target, hit.x, hit.y, dedupe_window):
            return None
        self._remember(hit.target, hit.x, hit.y)
        return hit

    def remember(self, target: str, x: int, y: int) -> None:
        """外部直接点击后登记，配合 dedupe_window 防止重复。"""
        self._remember(target, x, y)

    # ---------- 各层级 ----------
    def _by_attr(self, target: str, rows: Sequence[Dict[str, Any]]) -> List[Hit]:
        """L0：resource-id / content-desc 稳定属性。"""
        ids = [i.lower() for i in self.target_ids.get(target, []) if i]
        descs = [normalize_text(d) for d in self.target_descs.get(target, []) if d]
        out: List[Hit] = []
        for row in rows:
            if self._skip_row(row):
                continue
            rid = (row.get("resource_id") or "").lower()
            desc = normalize_text(row.get("desc") or "")
            hit_id = any(key in rid for key in ids)
            hit_desc = any(key and (key == desc or key in desc) for key in descs)
            if hit_id or hit_desc:
                label = row.get("desc") or row.get("resource_id") or row.get("text") or ""
                out.append(Hit(target, row["cx"], row["cy"], "L0", "attr", str(label), 1.0, row))
        return out

    def _by_text(self, target: str, rows: Sequence[Dict[str, Any]]) -> List[Hit]:
        """L1：控件 text 精确 + 模糊匹配。"""
        out: List[Hit] = []
        for row in rows:
            if self._skip_row(row):
                continue
            text = row.get("text") or row.get("desc") or ""
            if text_match(text, target, self.fuzzy):
                out.append(Hit(target, row["cx"], row["cy"], "L1", "text", str(text), 0.95, row))
        return out

    def _by_template(self, target: str, screen: Any) -> List[Hit]:
        """L2：模板匹配，适合 Flutter/Canvas/WebView 等无文本按钮。"""
        if screen is None:
            return []
        try:
            import cv2
            import numpy as np  # noqa: F401
        except Exception:
            return []
        templates = self._load_templates(target)
        if not templates:
            return []
        out: List[Hit] = []
        sh, sw = screen.shape[:2]
        for name, tpl in templates:
            for scale in (0.9, 1.0, 1.1):
                try:
                    scaled = tpl if scale == 1.0 else cv2.resize(
                        tpl, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA
                    )
                    if scaled.shape[0] > sh or scaled.shape[1] > sw:
                        continue
                    result = cv2.matchTemplate(screen, scaled, cv2.TM_CCOEFF_NORMED)
                    _, max_val, _, max_loc = cv2.minMaxLoc(result)
                    if max_val < self.template_threshold:
                        continue
                    x = int(max_loc[0] + scaled.shape[1] / 2)
                    y = int(max_loc[1] + scaled.shape[0] / 2)
                    out.append(Hit(target, x, y, "L2", "template", name, float(max_val)))
                except Exception:
                    continue
        return out

    def _load_templates(self, target: str):
        """加载并缓存目标模板；文件名含目标名或别名即可。"""
        if target in self._template_cache:
            return self._template_cache[target]
        result = []
        if os.path.isdir(self.template_dir):
            try:
                import cv2
                for name in os.listdir(self.template_dir):
                    if not name.lower().endswith((".png", ".jpg", ".jpeg")):
                        continue
                    stem = os.path.splitext(name)[0]
                    if target not in stem and normalize_text(target) not in normalize_text(stem):
                        continue
                    img = cv2.imread(os.path.join(self.template_dir, name), cv2.IMREAD_COLOR)
                    if img is not None:
                        result.append((name, img))
            except Exception:
                result = []
        self._template_cache[target] = result
        return result

    def _by_ocr(
        self,
        target: str,
        ocr_items: Sequence[Tuple[str, float, float, float]],
        sw: int,
        sh: int,
    ) -> List[Hit]:
        """L3：OCR 文字兜底。ocr_items 元素为 (text, score, cx, cy)。"""
        out: List[Hit] = []
        for text, score, cx, cy in ocr_items:
            if text_match(text, target, self.fuzzy):
                out.append(Hit(target, int(cx), int(cy), "L3", "ocr", str(text), float(score)))
        return out

    def _by_ratio(self, target: str, sw: int, sh: int) -> List[Hit]:
        """L4：最后防线，按比例坐标点击。"""
        ratio = self.target_ratio.get(target)
        if ratio and sw and sh:
            return [Hit(target, int(sw * ratio[0]), int(sh * ratio[1]), "L4", "ratio", "固定比例坐标", 0.8)]
        return []

    # ---------- 辅助 ----------
    def _skip_row(self, row: Dict[str, Any]) -> bool:
        if self.clickable_only and str(row.get("clickable", "")).lower() != "true":
            return True
        if self.hide_system and str(row.get("package", "") or "").startswith(_SYSTEM_PREFIXES):
            return True
        return False

    def _in_roi(self, x: int, y: int, sw: int, sh: int, target: str) -> bool:
        if not sw or not sh:
            return True
        roi = self.target_roi.get(target)
        if not roi:
            return True
        x0, x1 = roi["x"]
        y0, y1 = roi["y"]
        return x0 * sw <= x <= x1 * sw and y0 * sh <= y <= y1 * sh

    def _is_top_right(self, row: Dict[str, Any], sw: int, sh: int) -> bool:
        if not sw or not sh:
            return False
        return row.get("cx", 0) >= sw * 0.72 and row.get("cy", 0) <= sh * 0.20

    def _is_dupe(self, target: str, x: int, y: int, window: float) -> bool:
        key = (target, int(x) // 20, int(y) // 20)
        return time.time() - self.last_click.get(key, 0) < window

    def _remember(self, target: str, x: int, y: int) -> None:
        self.last_click[(target, int(x) // 20, int(y) // 20)] = time.time()
