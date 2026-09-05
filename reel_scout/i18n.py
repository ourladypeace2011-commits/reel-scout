"""Shared UI-chrome translations for the inspector and the read-only viewer.

Single source of truth so the two pages cannot drift apart. Scope is interface
chrome ONLY — model output (reasoning, transcript, scene descriptions, and
decoded-structure VALUES like "educational") carries no data-i18n and is never
translated; doing so would mean re-running the model, which a language toggle
must not silently do.

Each page renders English as the baseline text of every labelled element (so it
still reads with JS off, and string-contains tests keep passing) and tags it with
a `data-i18n` key. applyLang() swaps textContent client-side. Chinese is
Traditional (zh-Hant) to match the rest of the toolchain.

## Decoded VALUES: the original rule was too wide

The first version refused to translate any model output, on the grounds that
translating it "would mean re-running the model". That reason holds for free
text — a summary, a hook line, a piece of reasoning — where the words are the
model's own and a translation would be a different claim.

It does not hold for the decoded enums. `content_structure`, `content_type`,
`format`, `pacing`, `opening_type` and `cta_type` come from closed vocabularies
written into the merge prompt (`analyze/merger.py`), and the model's job is to
pick one. Showing the picked one in Chinese is a display mapping, exactly like
the `row.*` and `dim.*` labels beside them — no model runs, and the stored value
never changes.

So the rule is now the narrower and more honest one:

    closed vocabulary  -> translatable (VALUE_KEYS below)
    free text          -> never       (opening_text, cta_text, summary,
                                       reasoning, transcript, topics, titles)

⚠️ A value that is not in `VALUE_KEYS` renders untouched in both languages. That
is deliberate: a vocabulary that grows in the merge prompt and not here should
show the raw value rather than a stale translation of a neighbouring one.
"""

STRINGS = {
    "en": {
        # --- inspector ---
        "brand": "reel-scout inspect",
        "allReels": "← all reels",
        "source": "source ↗",
        "noVideo": "video file not on disk — keyframes & transcript only",
        "waveform": "Waveform",
        "noIO": "no in/out",
        "in": "IN",
        "out": "OUT",
        "setIn": "set IN",
        "setOut": "set OUT",
        "clear": "clear",
        "exportSrt": "export SRT (window)",
        "keyframes": "Keyframes",
        "marks": "Marks",
        "seek": "click to seek",
        "described": "described",
        "transcript": "Transcript",
        # The glyph lives INSIDE the string, in both dicts: applyLang() swaps a
        # node's whole textContent, so anything parked outside the span would
        # survive one toggle and vanish on the next.
        "noTranscript": "\u26a0 no transcript",
        "noTranscriptNote": ("No transcript for this clip (music-only / no narration) "
                             "\u2014 the score draws on the visual layer and the "
                             "measured rhythm alone."),
        "craftScores": "Craft scores",
        "refNotAuthority": "reference, not authority",
        "reweightSummary": "Re-weight — see how much the verdict depends on what you value",
        "reweightNote": ("The four dimensions come from the model and do not change "
                         "here — only how they are combined. Weights are rescaled "
                         "to sum to 100%, so the result stays on the same 0–10 axis "
                         "as the stored score."),
        "reset": "reset to default",
        "wDefault": "default",
        "wYours": "yours",
        "zeroWeights": "all weights at zero — no verdict",
        "decoded": "Decoded structure",
        "shotGrammar": "Shot grammar",
        "analysable": "analysable",
        "dim.overall": "Overall",
        "dim.hook_strength": "Hook",
        "dim.visual_storytelling": "Visual",
        "dim.pacing": "Pacing",
        "dim.structure": "Structure",
        "row.Structure": "Structure",
        "row.Content": "Content",
        "row.Format": "Format",
        "row.Pacing": "Pacing",
        "row.Hook": "Hook",
        "row.Hook text": "Hook text",
        "row.CTA": "CTA",
        "row.CTA text": "CTA text",
        # --- viewer (library list + take-home bundle detail) ---
        "sub": "decoded structure · read-only",
        "emptyLibrary": "No analyzed videos yet.",
        "emptyBundle": "No analyzed videos to show.",
        "allVideos": "← all videos",
        "topics": "Topics:",
        "timeline": "Timeline",
        "craftScoresNote": "reference, not authority — human judgment leads",
        "imgUnavailable": "image unavailable",
        "onScreen": "on-screen:",
        "sourcePlain": "source",
        "row.Content type": "Content type",
        "row.Hook type": "Hook type",
        "row.CTA type": "CTA type",
        # --- library table + annotations (served list only, never the export) ---
        "subLive": "decoded structure · your notes save here",
        "col.title": "Title",
        "col.group": "Group",
        "col.score": "Score",
        "col.note": "Note",
        "groupNone": "— none —",
        "notePlaceholder": "what is this one for?",
        "newGroupPh": "new group name",
        "addGroup": "add group",
        "groupPick": "— pick a group —",
        "removeGroup": "delete group",
        "onlyStarred": "show starred only",
        "saved": "saved",
        "saveFailed": "not saved",
    },
    "zh": {
        # --- inspector ---
        "brand": "reel-scout 檢視",
        "allReels": "← 所有短片",
        "source": "來源 ↗",
        "noVideo": "影片檔不在磁碟，僅關鍵影格與逐字稿",
        "waveform": "波形",
        "noIO": "未設進出點",
        "in": "進",
        "out": "出",
        "setIn": "設進點",
        "setOut": "設出點",
        "clear": "清除",
        "exportSrt": "匯出 SRT（區間）",
        "keyframes": "關鍵影格",
        "marks": "標記",
        "seek": "點擊跳轉",
        "described": "有描述",
        "transcript": "逐字稿",
        "noTranscript": "\u26a0 無逐字稿",
        "noTranscriptNote": "這支沒有逐字稿（純音樂／無旁白），評分只依據畫面與節奏測量。",
        "craftScores": "工藝評分",
        "refNotAuthority": "參考，非定論",
        "reweightSummary": "重新加權 — 看評分有多取決於你重視什麼",
        "reweightNote": ("四個維度來自模型，在這裡"
                         "不會改變，只改變它們如何"
                         "組合。權重會重新縮放為總"
                         "和 100%，因此結果維持在與儲"
                         "存分數相同的 0–10 尺度上。"),
        "reset": "重設為預設",
        "wDefault": "預設",
        "wYours": "你的",
        "zeroWeights": "所有權重為零 — 無評分",
        "decoded": "解構分析",
        "shotGrammar": "鏡頭語法",
        "analysable": "可分析",
        "dim.overall": "總分",
        "dim.hook_strength": "開場鉤子",
        "dim.visual_storytelling": "視覺敘事",
        "dim.pacing": "節奏",
        "dim.structure": "結構",
        "row.Structure": "內容結構",
        "row.Content": "內容類型",
        "row.Format": "格式",
        "row.Pacing": "節奏",
        "row.Hook": "開場類型",
        "row.Hook text": "開場文字",
        "row.CTA": "行動呼籲",
        "row.CTA text": "行動呼籲文字",
        # --- viewer ---
        "sub": "解構分析 · 唯讀",
        "emptyLibrary": "尚無已分析的影片。",
        "emptyBundle": "沒有可顯示的已分析影片。",
        "allVideos": "← 所有影片",
        "topics": "主題：",
        "timeline": "時間軸",
        "craftScoresNote": "參考，非定論 — 由人的判斷主導",
        "imgUnavailable": "圖片無法顯示",
        "onScreen": "畫面文字：",
        "sourcePlain": "來源",
        "row.Content type": "內容類型",
        "row.Hook type": "開場類型",
        "row.CTA type": "行動呼籲類型",
        # --- 清單表格 + 註記（僅現場清單頁，匯出永遠不含） ---
        "subLive": "解構分析 · 你的註記會存下來",
        "col.title": "標題",
        "col.group": "分組",
        "col.score": "分數",
        "col.note": "備註",
        "groupNone": "— 未分組 —",
        "notePlaceholder": "這支要拿來做什麼？",
        "newGroupPh": "新分組名稱",
        "addGroup": "新增分組",
        "groupPick": "— 選一個分組 —",
        "removeGroup": "刪除分組",
        "onlyStarred": "只看已收藏",
        "saved": "已儲存",
        "saveFailed": "未儲存",
    },
}

#: Header language toggle, shared by both pages so it looks identical.
TOGGLE_HTML = ('<div class="lang" id="lang">'
               '<button type="button" class="langbtn" data-lang="en">EN</button>'
               '<button type="button" class="langbtn" data-lang="zh">中文</button></div>')

#: Toggle styling. Positioned by the host page; these are the button visuals.
TOGGLE_CSS = (
    ".lang{display:flex;gap:2px;font-family:var(--mono)}"
    ".lang .langbtn{border:1px solid var(--rule-soft);background:none;color:var(--quiet);"
    "font-family:inherit;font-size:10px;letter-spacing:.1em;padding:3px 8px;cursor:pointer;"
    "line-height:1.4}"
    ".lang .langbtn:first-child{border-radius:3px 0 0 3px}"
    ".lang .langbtn:last-child{border-radius:0 3px 3px 0;border-left:0}"
    ".lang .langbtn:hover{color:var(--ink)}"
    ".lang .langbtn.on{background:var(--ink);color:var(--bg);border-color:var(--ink)}"
)


def boot_island(element_id: str = "rsboot") -> str:
    """A JSON island carrying both dictionaries, so the toggle is a pure client
    swap and a frozen/offline export stays bilingual."""
    import json
    return ('<script id="%s" type="application/json">%s</script>'
            % (element_id, json.dumps({"i18n": STRINGS}, ensure_ascii=False)))


#: Self-contained toggle script for pages that have no other JS (the viewer).
#: The inspector keeps its own applyLang inline (it also drives waveform/reweight
#: dynamic strings); this is the ~stable boilerplate half, deliberately duplicated
#: rather than sharing a runtime, because the part that actually drifts — STRINGS —
#: is already shared above.
APPLY_JS = r"""
(function(){
  var island=document.getElementById('rsboot');
  if(!island) return;
  var I18N=JSON.parse(island.textContent).i18n||{};
  var LANG='en';
  function applyLang(lang){
    if(!I18N[lang]) lang='en';
    LANG=lang;
    var d=I18N[lang];
    document.documentElement.lang=(lang==='zh'?'zh-Hant':'en');
    [].forEach.call(document.querySelectorAll('[data-i18n]'),function(el){
      var k=el.getAttribute('data-i18n'); if(d[k]!=null) el.textContent=d[k];
    });
    // Attribute-carried chrome (a placeholder, a tooltip) needs its own pass —
    // textContent would wipe an <input>'s value or an <option> list instead.
    [].forEach.call(document.querySelectorAll('[data-i18n-ph]'),function(el){
      var k=el.getAttribute('data-i18n-ph'); if(d[k]!=null) el.placeholder=d[k];
    });
    [].forEach.call(document.querySelectorAll('[data-i18n-title]'),function(el){
      var k=el.getAttribute('data-i18n-title'); if(d[k]!=null) el.title=d[k];
    });
    [].forEach.call(document.querySelectorAll('#lang .langbtn'),function(b){
      b.classList.toggle('on', b.getAttribute('data-lang')===lang);
    });
    try{ localStorage.setItem('rs_lang', lang); }catch(e){}
  }
  [].forEach.call(document.querySelectorAll('#lang .langbtn'),function(b){
    b.addEventListener('click',function(){ applyLang(b.getAttribute('data-lang')); });
  });
  var init;
  try{ init=localStorage.getItem('rs_lang'); }catch(e){}
  if(!init) init=((navigator.language||'').toLowerCase().indexOf('zh')===0)?'zh':'en';
  applyLang(init);
})();
"""


#: Closed decoded vocabularies, exactly as `analyze/merger.py` defines them.
#: Kept as a flat set of values (not per-field) because `none` is shared by
#: `opening_type` and `cta_type` and means the same thing in both.
VALUE_KEYS = (
    # content_structure
    "hook-body-cta", "problem-solution", "listicle", "story-arc", "raw-moment",
    # content_type
    "educational", "entertainment", "promotional", "review", "story", "news",
    # style.format
    "talking_head", "montage", "tutorial", "reaction", "skit", "vlog", "slideshow",
    # style.pacing
    "fast", "medium", "slow",
    # hook.opening_type
    "question", "statement", "visual", "music",
    # hook.cta_type
    "follow", "like", "comment", "link", "visit",
    # shared by opening_type and cta_type
    "none",
    # shot_size (roadmap §6B). `UNKNOWN` earns a translation of its own: on
    # this corpus it is 69% of all labels, so "the question does not apply
    # here" is the most common thing the page says.
    "ECU", "CU", "MCU", "MS", "MLS", "LS", "ELS", "UNKNOWN",
    # shot movement (§6C)
    "static", "still_subject_moves", "camera_moves", "unsteady", "unknown",
    "unsupported",
)

#: Traditional Chinese for each. English is the value itself, so the baseline
#: render needs no lookup and the page still reads with JS off.
_VALUE_ZH = {
    "hook-body-cta": "鉤子-主體-CTA", "problem-solution": "問題-解方",
    "listicle": "清單式", "story-arc": "故事弧", "raw-moment": "生活片段",
    "educational": "教學", "entertainment": "娛樂", "promotional": "宣傳",
    "review": "評測", "story": "故事", "news": "新聞",
    "talking_head": "說話人", "montage": "剪輯串接", "tutorial": "教學示範",
    "reaction": "反應", "skit": "短劇", "vlog": "vlog", "slideshow": "圖卡",
    "fast": "快", "medium": "中", "slow": "慢",
    "question": "提問", "statement": "陳述", "visual": "畫面", "music": "音樂",
    "follow": "追蹤", "like": "按讚", "comment": "留言", "link": "連結",
    "ECU": "大特寫", "CU": "特寫", "MCU": "中特寫", "MS": "中景",
    "MLS": "中遠景", "LS": "遠景", "ELS": "大遠景", "UNKNOWN": "不適用",
    "static": "固定", "still_subject_moves": "機位不動·畫面內有動作",
    "camera_moves": "運鏡", "unsteady": "手持/不穩", "unknown": "幀數不足",
    "unsupported": "此編碼無運動向量",
    "visit": "到店",
    "none": "無",
}


def value_key(value):
    """The i18n key for a decoded value, or None when it is not a known enum.

    Returning None is what keeps free text out: a hook line or a title never
    matches, so it is rendered raw and stays raw in both languages.
    """
    if not value:
        return None
    v = str(value).strip()
    return ("val.%s" % v) if v in VALUE_KEYS else None


for _v in VALUE_KEYS:
    STRINGS["en"]["val.%s" % _v] = _v
    STRINGS["zh"]["val.%s" % _v] = _VALUE_ZH[_v]
