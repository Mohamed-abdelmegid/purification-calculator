# -*- coding: utf-8 -*-
"""محرّك تحليل القوائم المالية للبنوك الإسلامية (عربي / إنجليزي، نصي أو ممسوح ضوئياً).

الفكرة:
1. قراءة نص الـ PDF بسرعة (pypdfium2). لو الملف صور فقط -> OCR (tesseract) على الصفحات المطلوبة فقط.
2. إيجاد قائمة الدخل وقراءة بنود الإيراد (عائد المرابحات، الأتعاب، التوزيعات، المتاجرة، أرباح الاستثمارات).
3. إيجاد إيضاح "صافي الدخل من العائد" وتفصيل عائد المرابحات (عملاء / بنوك / أدوات دين / ...).
4. تصنيف كل بند، والتحقق أن مجموع التفصيل = الرقم في قائمة الدخل.
5. نسبة التطهير = الإيراد غير المتوافق ÷ إجمالي الإيرادات (قبل خصم تكلفة الودائع).

قاعدة الأحوط: أذون وسندات الخزانة وأدوات الدين الحكومية وودائع/أرصدة البنوك = غير متوافق.
"""
import re
import shutil
import hashlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import pypdfium2 as pdfium

NON, REVIEW, OK = "NON_COMPLIANT", "REVIEW", "COMPLIANT"
LABEL_AR = {NON: "غير متوافق", REVIEW: "يحتاج مراجعة", OK: "متوافق"}
LABEL_EN = {v: k for k, v in LABEL_AR.items()}

# ---------------------------------------------------------------- نصوص
_DIG = str.maketrans("٠١٢٣٤٥٦٧٨٩٬٫", "0123456789,.")
_NORM = [(re.compile("[ً-ْـ]"), ""), (re.compile("[إأآ]"), "ا"),
         (re.compile("ى"), "ي"), (re.compile("ة"), "ه")]
_AR = re.compile("[؀-ۿ]")


def norm(text):
    t = text.translate(_DIG)
    for rx, rep in _NORM:
        t = rx.sub(rep, t)
    return re.sub(r"\s+", "", t).lower()


def has_ar(s):
    return bool(_AR.search(s))


# ---------------------------------------------------------------- مبالغ
_AMT = re.compile(r"^\(?-?\d[\d,.]*\)?$")
_NOTE = re.compile(r"^\(?\d{1,2}\)?$")
_DASH = {"-", "—", "–"}


def _to_float(tok):
    neg = tok.startswith("(") or tok.startswith("-")
    t = tok.strip("()-")
    if re.fullmatch(r"\d{1,3}(\.\d{3})+", t):          # OCR: 4.814.734
        t = t.replace(".", "")
    t = t.replace(",", "")
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def split_row(line):
    """يرجع (label, [amounts]) — أول رقم = السنة/الفترة الحالية. بيتجاهل رقم الإيضاح."""
    toks = line.replace("‏", " ").replace("‎", " ").split()
    kinds = []
    for t in toks:
        t2 = t.translate(_DIG)
        if len(t2) > 2 and t2.startswith(")") and t2.endswith("("):
            t2 = "(" + t2[1:-1] + ")"          # أقواس معكوسة في النص العربي
        if t2 in _DASH:
            kinds.append(("d", t2))
        elif _AMT.match(t2) and re.search(r"\d", t2):
            kinds.append(("n", t2))
        else:
            kinds.append(("l", t))
    label, raw = [], []
    for i, (k, t) in enumerate(kinds):
        if k == "d":
            near = (i > 0 and kinds[i - 1][0] in "nd") or (i + 1 < len(kinds) and kinds[i + 1][0] in "nd")
            if near:
                raw.append("-")
            continue                       # شرطة bullet تتجاهل
        if k == "n":
            raw.append(t)
        else:
            label.append(t)
    if has_ar(line):                 # pdfium بيرجّع أعمدة السطر العربي معكوسة
        raw = raw[::-1]
    if len(raw) == 1 and _NOTE.match(raw[0]) and len(re.sub(r"[^A-Za-z\u0600-\u06FF]", "", " ".join(label))) >= 6:
        raw = []                       # "similar revenues (6)": رقم إيضاح مش مبلغ
    if len(raw) >= 3 and _NOTE.match(raw[0]):
        raw = raw[1:]
    amts = []
    for r in raw:
        if r == "-":
            amts.append(0.0)
        else:
            v = _to_float(r)
            if v is not None:
                amts.append(v)
    return " ".join(label), amts


_HDR = ("thousand", "egp", "ديسم", "يوني", "جني", "months", "december", "june", "march", "september",
        "اشهر", "contentspage", "الف")


def _is_header(line):
    n = norm(line)
    return any(h in n for h in _HDR) and not any(k in n for k in ("murabaha", "مرابحات"))


def logical_rows(text):
    """يحوّل نص الصفحة لصفوف منطقية: (label, amounts, header) — بيجمّع التسمية الموزعة على أكتر من سطر
    مع سطر الأرقام، ويرجّع الأسطر العنوانية (بدون أرقام) برضه بـ amounts فاضية."""
    out, buf = [], []
    for line in text.split("\n"):
        line = line.strip()
        if not line or _is_header(line):
            continue
        label, amts = split_row(line)
        if not amts:
            if label.strip():
                buf.append(label)
                out.append((label, [], "", label))
                buf = buf[-3:]
            continue
        letters = len(re.sub(r"[^A-Za-z\u0600-\u06FF]", "", label))
        full = label if letters >= 12 else " ".join(buf + [label])
        out.append((full, amts, " ".join(buf), label))
        buf = []
    return out


def cur_prev(amts):
    """السنة الحالية والسابقة. قوائم دورية (4 أعمدة): 6 شهور حالية ثم 6 شهور سابقة."""
    if len(amts) >= 4:
        return amts[0], amts[2]
    if len(amts) >= 2:
        return amts[0], amts[1]
    if len(amts) == 1:
        return amts[0], 0.0
    return 0.0, 0.0


# ---------------------------------------------------------------- الملف
@dataclass
class Row:
    label: str
    cur: float
    prev: float
    status: str
    why: str = ""


@dataclass
class Result:
    name: str = ""
    method: str = "text"
    period: str = ""
    is_page: int = 0
    note_page: int = 0
    income: dict = field(default_factory=dict)       # key -> (label, cur, prev)
    rows: list = field(default_factory=list)         # [Row] مكونات عائد المرابحات
    warnings: list = field(default_factory=list)
    scanned: bool = False


def read_pages(path):
    pdf = pdfium.PdfDocument(path)
    out = []
    for i in range(len(pdf)):
        try:
            out.append(pdf[i].get_textpage().get_text_bounded().replace("\r", ""))
        except Exception:
            out.append("")
    pdf.close()
    return out


_WIN_PATHS = [r"C:\Program Files\Tesseract-OCR\tesseract.exe", r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
              os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe")]
_render_lock = threading.Lock()


def tesseract_cmd():
    """مسار tesseract (PATH أو أماكن التثبيت الافتراضية في ويندوز). None لو مش مثبّت."""
    found = shutil.which("tesseract")
    if found:
        return found
    for p in _WIN_PATHS:
        if os.path.exists(p):
            return p
    return None


def _ocr_job(args):
    path, pno, dpi, lang, psm = args
    import pytesseract
    cmd = tesseract_cmd()
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd
    with _render_lock:                      # pdfium مش آمنة مع أكتر من thread في نفس الوقت
        pdf = pdfium.PdfDocument(path)
        im = pdf[pno - 1].render(scale=dpi / 72).to_pil().convert("L")
        pdf.close()
    return pno, pytesseract.image_to_string(im, lang=lang, config=f"--psm {psm}")


def ocr_pages(path, pnos, dpi=110, lang="eng", psm=4, workers=2):
    args = [(path, p, dpi, lang, psm) for p in pnos]
    if len(args) == 1 or workers == 1:
        return dict(_ocr_job(a) for a in args)
    with ThreadPoolExecutor(workers) as ex:
        return dict(ex.map(_ocr_job, args))


# ---------------------------------------------------------------- قائمة الدخل
def _is_fee_income(n):
    return ("fee" in n and "income" in n and "net" not in n and "expense" not in n) or \
           ("تعاب" in n and "ايرادات" in n)


IS_KEYS = [
    ("finance_income", "عائد المرابحات والمضاربات والمشاركات والإيرادات المشابهة",
     lambda n: "murabaha" in n or "مرابحات" in n),
    ("fee_income", "إيرادات الأتعاب والعمولات", _is_fee_income),
    ("dividends", "توزيعات الأرباح", lambda n: "dividend" in n or "توزيعات" in n),
    ("trading", "صافي دخل المتاجرة", lambda n: "trading" in n or "المتاجره" in n),
    ("inv_gains", "أرباح استثمارات مالية",
     lambda n: ("gain" in n and "investment" in n) or ("ارباح" in n and "ستثمارات" in n)),
]


def parse_income_statement(text):
    found = {}
    for label, amts, _, _own in logical_rows(text):
        if not amts:
            continue
        n = norm(label)
        for key, title, test in IS_KEYS:
            if key in found or not test(n):
                continue
            if key == "finance_income" and max(abs(a) for a in amts) < 1000:
                continue
            found[key] = (title, *cur_prev(amts))
    return found


def _fmt(x):
    return f"{int(round(x)):,}"


# ---------------------------------------------------------------- الإيضاح + التصنيف
_TOTAL = ("total", "الاجمالي", "اجمالي", "جما", "المجموع", "الجمله")
_STOP = ("costof", "تكلفهالودايع", "تكلفهالودائع", "تكلفهالودايع", "تكلفهالودائع", "costofdeposit", "netrevenues", "netincomefrom", "netinterest", "صافيالدخل")


def classify(label, section):
    n = norm(label)
    low = label.lower()
    if re.search(r"non[\s-]*government", low) or ("غير" in n and "حكومي" in n):
        return REVIEW, "أدوات دين غير حكومية - تحتاج مراجعة (صكوك أم سندات تقليدية؟)"
    if "centralbank" in n or "المركزي" in n:
        return REVIEW, "عائد لدى البنك المركزي - يحتاج مراجعة"
    if any(k in n for k in ("treasury", "اذون", "خزانه", "bond", "سندات")) or \
       ("debt" in n and "instrument" in n) or ("دين" in n and "ادوات" in n) or ("دين" in n and "اداه" in n):
        return NON, "أذون/سندات خزانة وأدوات دين - غير متوافق (الأحوط)"
    if "deposit" in n or "ودائع" in n or "placement" in n or "currentaccount" in n:
        return NON, "ودائع وأرصدة لدى البنوك - غير متوافق"
    if "bank" in n or "بنوك" in n or "بنك" in n:
        if any(k in n for k in ("financing", "facilit", "تمويل", "تسهيل")):
            return OK, "تمويل مقدم لبنوك (مرابحة) - متوافق"
        return NON, "عائد من بنوك أخرى - غير متوافق"
    if "customer" in n or "عمال" in n or "عملاء" in n or "financing" in n or "تمويل" in n or "مرابحه" in n:
        return OK, "تمويل للعملاء - متوافق"
    if section and section != label:
        st, why = classify(section, "")
        if "غير معروف" not in why:
            return st, why
    return REVIEW, "بند غير معروف - يحتاج مراجعة"


def parse_note(text, finance_cur):
    """يقرأ تفصيل عائد المرابحات. بيرجع (rows, sum_cur)."""
    rows, running, started = [], 0.0, False
    for label, amts, section, own in logical_rows(text):
        n = norm(label)
        if not started:
            if "murabaha" in n or "مرابحات" in n:
                started = True
            if not amts:
                continue
            if not started:
                continue
        if any(x in n for x in _STOP):
            break
        if not amts:
            continue
        if any(t in n for t in _TOTAL):
            continue
        letters = re.sub(r"[^A-Za-z\u0600-\u06FF]", "", label)
        first = re.sub(r"[^a-z]", "", (label.split() or [""])[0].lower())
        if not has_ar(label) and first.startswith("to") and 3 <= len(first) <= 6:
            continue                                        # Total مقروءة غلط بالـ OCR
        cur, prev = cur_prev(amts)
        if rows and abs(cur - running) < 1.5 and cur != 0:
            continue                                        # إجمالي فرعي
        st, why = classify(label, section)
        rows.append(Row((own or label).strip(" :-"), cur, prev, st, why))
        running += cur
        if finance_cur and abs(running - finance_cur) < 1.5:
            break
    return rows, running


# ---------------------------------------------------------------- التحليل
def _find_is_page(texts):
    for i, t in enumerate(texts):
        inc = parse_income_statement(t)
        if len(inc) >= 4 and "finance_income" in inc:
            return i + 1, inc
    return 0, {}


def _find_note_candidates(texts, is_page, finance_cur, finance_prev):
    cands = []
    keys = [_fmt(finance_cur)]
    for i, t in enumerate(texts):
        if i + 1 == is_page:
            continue
        if any(k in t for k in keys):
            n = norm(t)
            if ("murabaha" in n or "مرابحات" in n) and ("costof" in n or "تكلفه" in n or "cost" in n):
                cands.append(i + 1)
    return cands


def _title(texts):
    head = " ".join(texts[0].split())[:160] if texts else ""
    return head


OCR_HELP = ("هذا الملف صور (ممسوح ضوئياً) ويحتاج برنامج Tesseract لقراءته، وهو غير مثبّت على هذا الجهاز. "
            "ثبّته من github.com/UB-Mannheim/tesseract/wiki (اختر Arabic في اللغات) ثم أعد تشغيل الأداة، "
            "أو انشر الأداة على Streamlit Cloud حيث يُثبَّت تلقائياً، أو أدخل الأرقام يدوياً.")

VISION_PROMPT = (
    "الصورة صفحة من إيضاحات قوائم مالية لبنك إسلامي. ابحث عن جدول تفصيل (عائد المرابحات والمضاربات والمشاركات والإيرادات المشابهة) "
    "واستخرج البنود الفرعية فقط (بدون صفوف الإجمالي) بالأرقام كما هي بالألف جنيه، السنة الحالية ثم السابقة. "
    'أجب JSON فقط: [{"label":"...","cur":123,"prev":123}] — التسمية كما في الجدول. لا تخمّن أرقاماً غير ظاهرة.')


def vision_rows(path, page, api_key, model="claude-sonnet-5-5"):
    import base64, io, json
    import anthropic
    pdf = pdfium.PdfDocument(path)
    im = pdf[page - 1].render(scale=160 / 72).to_pil()
    pdf.close()
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    msg = anthropic.Anthropic(api_key=api_key).messages.create(
        model=model, max_tokens=1500,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": base64.b64encode(buf.getvalue()).decode()}},
            {"type": "text", "text": VISION_PROMPT}]}])
    txt = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    data = json.loads(txt[txt.find("["): txt.rfind("]") + 1])
    rows = []
    for d in data:
        st, why = classify(str(d.get("label", "")), "")
        rows.append(Row(str(d.get("label", "")), float(d.get("cur") or 0), float(d.get("prev") or 0), st, why))
    return rows


def _check_sums(res, fin):
    for idx, nm in ((0, "الحالية"), (1, "السابقة")):
        s = sum((r.cur if idx == 0 else r.prev) for r in res.rows)
        if abs(s - fin[1 + idx]) >= 1.5:
            res.warnings.append(
                f"السنة {nm}: مجموع مكونات الإيضاح ({_fmt(s)}) لا يساوي عائد المرابحات في قائمة الدخل "
                f"({_fmt(fin[1 + idx])})" + (" - غالباً خطأ قراءة في الـ OCR، صحّح الأرقام من الجدول." if res.method == "ocr" else "."))


def _doc_is_arabic(texts):
    sample = " ".join(t for t in texts if t.strip())[:20000]
    letters = len(re.findall(r"[A-Za-z\u0600-\u06FF]", sample))
    return letters > 200 and len(_AR.findall(sample)) / letters > 0.4


def analyze(path, allow_ocr=True, progress=None, api_key=None):
    say = progress or (lambda m: None)
    res = Result(name=path.split("/")[-1])
    texts = read_pages(path)
    n_pages = len(texts)
    weak = [i + 1 for i, t in enumerate(texts) if len(t.strip()) < 40]
    res.scanned = len(weak) > n_pages * 0.5
    res.period = _title(texts)
    ocr_ok = allow_ocr and bool(tesseract_cmd())

    say("قراءة قائمة الدخل")
    res.is_page, res.income = _find_is_page(texts)

    # قائمة الدخل صورة؟ -> OCR على الصفحات الأولى (إنجليزي فقط)
    if not res.income:
        front_weak = [p for p in range(3, min(n_pages, 16) + 1) if len(texts[p - 1].strip()) < 400]
        if not front_weak:
            res.warnings.append("لم أجد قائمة الدخل. أدخل الأرقام يدوياً.")
            return res
        if not ocr_ok:
            res.warnings.append(OCR_HELP)
            return res
        res.method = "ocr"
        try:
            head = " ".join(ocr_pages(path, [1, 2, 3], dpi=80, lang="ara+eng", psm=3).values())
        except Exception:                      # حزمة اللغة العربية غير مثبتة
            head = " ".join(ocr_pages(path, [1, 2, 3], dpi=80, lang="eng", psm=3).values())
        n_ar, n_en = len(_AR.findall(head)), len(re.findall("[A-Za-z]", head))
        if _doc_is_arabic(texts) or n_ar > n_en * 0.8:
            res.warnings.append("قائمة الدخل صورة وبالعربي، وقراءة الأرقام العربية بالـ OCR غير موثوقة. "
                                "استخدم النسخة الإنجليزية من نفس القوائم أو أدخل الأرقام يدوياً.")
            return res
        say("OCR: البحث عن قائمة الدخل")
        cand = front_weak if not res.scanned else list(range(3, min(n_pages, 16) + 1))
        found = ocr_pages(path, cand, dpi=120)
        for p in cand:
            if len(parse_income_statement(found[p])) >= 4:
                res.is_page = p
                break
        if not res.is_page:
            res.warnings.append("لم أجد قائمة الدخل في الصفحات الأولى.")
            return res
        say("OCR: قراءة قائمة الدخل بدقة أعلى")
        res.income = parse_income_statement(ocr_pages(path, [res.is_page], dpi=250, psm=6)[res.is_page])
        if "finance_income" not in res.income:
            res.warnings.append("تعذرت قراءة عائد المرابحات من الصورة.")
            return res

    fin = res.income["finance_income"]
    say("قراءة إيضاح عائد المرابحات")
    best = None
    for pg in _find_note_candidates(texts, res.is_page, fin[1], fin[2]):
        rows, s = parse_note(texts[pg - 1], fin[1])
        if rows and abs(s - fin[1]) < 1.5:
            res.note_page, res.rows = pg, rows
            _check_sums(res, fin)
            return res
        if rows and best is None:
            best = (pg, rows)
    if best:
        res.note_page, res.rows = best
        _check_sums(res, fin)
        return res

    # الإيضاح صورة: نبحث في الصفحات ضعيفة النص بعد قائمة الدخل
    pages = [p for p in range(res.is_page + 1, n_pages + 1) if len(texts[p - 1].strip()) < 400]
    arabic_doc = _doc_is_arabic(texts)
    if not pages:
        res.warnings.append("لم أجد تفصيل عائد المرابحات في الإيضاحات. أدخل المكونات يدوياً.")
        return res
    if arabic_doc:
        # عنوان الإيضاح بالعربي يظهر كنص (الجدول صورة). نحدد الصفحة بالعنوان ونستعمل الذكاء الاصطناعي لو متاح
        cand = [p for p in pages if "العائد" in norm(texts[p - 1]) and "الدخل" in norm(texts[p - 1])]
        pg = cand[0] if cand else 0
        if pg and api_key:
            try:
                say("قراءة جدول الإيضاح بالذكاء الاصطناعي")
                res.rows, res.note_page, res.method = vision_rows(path, pg, api_key), pg, "vision"
                _check_sums(res, fin)
                return res
            except Exception as e:
                res.warnings.append(f"فشلت قراءة الجدول بالذكاء الاصطناعي: {e}")
        res.note_page = pg
        res.warnings.append(
            f"جدول إيضاح عائد المرابحات صورة داخل الـ PDF (صفحة {pg or '؟'}) وبالعربي، والـ OCR العربي غير موثوق. "
            "أدخل المكونات يدوياً، أو استخدم النسخة الإنجليزية، أو فعّل قراءة الذكاء الاصطناعي بمفتاح API.")
        return res
    if not ocr_ok:
        res.warnings.append(OCR_HELP)
        return res
    res.method = "ocr"
    say("OCR: البحث عن إيضاح عائد المرابحات")
    for k in range(0, len(pages), 8):
        chunk = pages[k:k + 8]
        low = ocr_pages(path, chunk, dpi=105)
        for p in chunk:
            n = norm(low[p])
            if ("murabaha" in n or "revenuesfromfunds" in n or "incomefromfunds" in n) and \
               len(re.findall(r"\d{1,3}(?:[,.]\d{3}){2,}", low[p])) >= 5:
                say("OCR: قراءة الإيضاح بدقة أعلى")
                txt = ocr_pages(path, [p], dpi=250, psm=6)[p]
                rows, s = parse_note(txt, fin[1])
                if rows:
                    res.note_page, res.rows = p, rows
                    _check_sums(res, fin)
                    return res
    res.warnings.append("لم أجد إيضاح تفصيل عائد المرابحات. أدخل المكونات يدوياً.")
    return res


# ---------------------------------------------------------------- النسب
def totals(res_income, idx):
    return sum(v[1 + idx] for v in res_income.values())


def ratios(income, rows, idx, other_review=0.0):
    """إجمالي الإيرادات = مجموع بنود الإيراد الخمسة (إجمالي، قبل خصم تكلفة الودائع)."""
    total = sum(v[1 + idx] for v in income.values())
    get = lambda r: r.cur if idx == 0 else r.prev
    non = sum(get(r) for r in rows if r.status == NON)
    rev = sum(get(r) for r in rows if r.status == REVIEW)
    if "inv_gains" in income:
        rev += income["inv_gains"][1 + idx]          # أرباح الاستثمارات: تركيبها غير مفصّل -> مراجعة
    f = lambda x: x / total if total else 0.0
    return {"total": total, "non": non, "review": rev,
            "strict_ratio": f(non), "broad_ratio": f(non + rev)}


def file_key(b):
    return hashlib.md5(b).hexdigest()
