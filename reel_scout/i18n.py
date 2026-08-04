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
