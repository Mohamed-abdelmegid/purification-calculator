# -*- coding: utf-8 -*-
import io
import os
import tempfile

import pandas as pd
import streamlit as st

import engine as E

st.set_page_config(page_title="حاسبة نسبة التطهير", layout="wide")
st.markdown("<style>html,body,[class*='css']{direction:rtl;text-align:right}</style>", unsafe_allow_html=True)
st.title("حاسبة نسبة تطهير الودائع - البنوك الإسلامية")
st.caption("نسبة التطهير = الإيراد غير المتوافق مع الشريعة ÷ إجمالي الإيرادات (قبل خصم تكلفة الودائع). "
           "أداة مساعدة، والاعتماد النهائي لقرار الهيئة الشرعية للبنك.")
st.info("**قاعدة الأحوط المطبقة:** أذون وسندات الخزانة وأدوات الدين الحكومية وعوائد الودائع والأرصدة لدى البنوك تُعتبر "
        "إيراداً غير متوافق. بنود البنك المركزي وأدوات الدين غير الحكومية وأرباح الاستثمارات تظهر في السيناريو الأوسع فقط. "
        "تقدر تغيّر تصنيف أي بند من الجدول.")

with st.sidebar:
    st.header("الإعدادات")
    allow_ocr = st.checkbox("قراءة الملفات الممسوحة ضوئياً (OCR) - أبطأ", value=True)
    api_key = st.text_input("ANTHROPIC_API_KEY (اختياري لقراءة جداول مصورة بالعربي)", type="password",
                            value=os.environ.get("ANTHROPIC_API_KEY", ""))
    st.caption("الملفات النصية تُحلَّل في أقل من ثانية. الملفات الممسوحة (صور) تأخذ حوالي دقيقة.")


@st.cache_data(show_spinner=False)
def run_analysis(data: bytes, name: str, allow_ocr: bool, api_key: str):
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(data)
    try:
        res = E.analyze(tmp.name, allow_ocr=allow_ocr, api_key=api_key or None)
        res.name = name
        return res
    finally:
        os.unlink(tmp.name)


files = st.file_uploader("ارفع القوائم المالية (PDF عربي أو إنجليزي، نصي أو ممسوح) - ممكن أكتر من ملف",
                         type=["pdf"], accept_multiple_files=True)
if not files:
    st.stop()

results = []
for f in files:
    with st.spinner(f"تحليل {f.name} ..."):
        results.append(run_analysis(f.getvalue(), f.name, allow_ocr, api_key))

IS_TITLES = {k: t for k, t, _ in E.IS_KEYS}

# أرقام راجعتها يدوياً من القوائم (بالألف جنيه) - تُستخدم لو الملف صور والـ OCR غير متاح
PRESETS = {
    "بنك البركة مصر - ديسمبر 2025": {
        "income": {"finance_income": (22470058, 18904303), "fee_income": (1202643, 974612), "dividends": (30153, 23869),
                   "trading": (210121, 165759), "inv_gains": (71742, 0)},
        "rows": [("تمويلات للعملاء", 12864841, 10066105, E.OK), ("استثمارات في أدوات دين (أذون وسندات)", 6919966, 5944102, E.NON),
                 ("ودائع وحسابات لدى البنوك", 2685251, 2894096, E.NON)]},
    "فيصل الإسلامي - 6 شهور حتى 30 يونيو 2026": {
        "income": {"finance_income": (14539898, 12933247), "fee_income": (361467, 261687), "dividends": (428899, 279037),
                   "trading": (228324, 56272), "inv_gains": (0, 0)},
        "rows": [("البنك المركزي المصري", 264087, 1104785, E.REVIEW), ("بنوك أخرى", 1865327, 1618119, E.NON),
                 ("عملاء", 1618758, 1994122, E.OK), ("عائد أدوات دين حكومية (أذون وسندات)", 10380507, 7894141, E.NON),
                 ("عائد أدوات دين غير حكومية", 411219, 322080, E.REVIEW)]},
    "ADIB مصر (أبوظبي الإسلامي) - ديسمبر 2025": {
        "income": {"finance_income": (48480843, 36363609), "fee_income": (3542631, 2550938), "dividends": (5774, 4247),
                   "trading": (524192, 672600), "inv_gains": (0, 2488)},
        "rows": [("تمويل لبنوك", 248476, 126917, E.OK), ("تمويل للعملاء", 25309572, 18265934, E.OK),
                 ("استثمارات في أدوات دين", 253603, 38992, E.NON), ("ودائع وحسابات جارية لدى البنوك", 22669192, 17931766, E.NON)]},
}
summary, export = [], {}
tabs = st.tabs([f"{i + 1}. {r.name[:28]}" for i, r in enumerate(results)])

for i, (tab, res) in enumerate(zip(tabs, results)):
    with tab:
        st.caption(f"{res.period[:140]}")
        st.caption(f"طريقة القراءة: {'OCR' if res.method == 'ocr' else 'نص مباشر' if res.method == 'text' else 'ذكاء اصطناعي'}"
                   f" | قائمة الدخل: صفحة {res.is_page or '-'} | الإيضاح: صفحة {res.note_page or '-'}")
        pre = st.selectbox("أرقام جاهزة (راجعتها يدوياً) - استخدمها لو الملف لم يُقرأ", ["- لا -"] + list(PRESETS),
                           key=f"pre{i}")
        P = PRESETS.get(pre)
        if not P:
            for w in res.warnings:
                st.warning(w)
        inc_src = {k: (IS_TITLES[k], *v) for k, v in P["income"].items()} if P else res.income
        row_src = [E.Row(a, b, c, d, "أرقام جاهزة") for a, b, c, d in P["rows"]] if P else res.rows

        st.markdown("**1) بنود الإيراد من قائمة الدخل (بالألف جنيه)**")
        is_df = pd.DataFrame([{"البند": IS_TITLES[k], "الفترة الحالية": inc_src.get(k, ("", 0, 0))[1],
                               "الفترة السابقة": inc_src.get(k, ("", 0, 0))[2]} for k in IS_TITLES])
        is_ed = st.data_editor(is_df, hide_index=True, disabled=["البند"], key=f"is{i}{pre}", num_rows="fixed")
        income = {k: (IS_TITLES[k], float(r["الفترة الحالية"]), float(r["الفترة السابقة"]))
                  for k, (_, r) in zip(IS_TITLES, is_ed.iterrows())}

        st.markdown("**2) تفصيل عائد المرابحات والتصنيف**")
        base = row_src or [E.Row("تمويلات للعملاء", 0, 0, E.OK), E.Row("ودائع وأرصدة لدى البنوك", 0, 0, E.NON),
                            E.Row("أدوات دين / أذون خزانة", 0, 0, E.NON)]
        df = pd.DataFrame([{"البند": r.label, "الفترة الحالية": r.cur, "الفترة السابقة": r.prev,
                            "التصنيف": E.LABEL_AR[r.status], "السبب": r.why} for r in base])
        ed = st.data_editor(
            df, hide_index=True, num_rows="dynamic", key=f"rows{i}{pre}", disabled=["السبب"],
            column_config={"التصنيف": st.column_config.SelectboxColumn(options=list(E.LABEL_AR.values()), required=True)})
        rows = [E.Row(str(r["البند"]), float(r["الفترة الحالية"] or 0), float(r["الفترة السابقة"] or 0),
                      E.LABEL_EN.get(r["التصنيف"], E.REVIEW)) for _, r in ed.iterrows() if str(r["البند"]).strip()]

        cur = E.ratios(income, rows, 0)
        prev = E.ratios(income, rows, 1)
        st.markdown("**3) النتيجة**")
        c1, c2, c3 = st.columns(3)
        c1.metric("إجمالي الإيرادات", f"{cur['total']:,.0f}")
        c2.metric("الإيراد غير المتوافق (الأحوط)", f"{cur['non']:,.0f}")
        c3.metric("نسبة التطهير (الأحوط)", f"{cur['strict_ratio'] * 100:.2f}%")
        st.markdown(
            f"- **السيناريو الأوسع** (+ بنود المراجعة): **{cur['broad_ratio'] * 100:.2f}%**\n"
            f"- **الفترة السابقة:** الأحوط {prev['strict_ratio'] * 100:.2f}% | الأوسع {prev['broad_ratio'] * 100:.2f}%")
        amt = st.number_input("طبّق النسبة على مبلغ العائد/الوديعة", value=100000.0, step=1000.0, key=f"amt{i}")
        st.write(f"المبلغ الواجب توجيهه لأوجه الخير: **{cur['strict_ratio'] * amt:,.2f}**")

        summary.append({"الملف": res.name, "إجمالي الإيرادات": cur["total"], "غير المتوافق (الأحوط)": cur["non"],
                        "النسبة (الأحوط)": cur["strict_ratio"], "النسبة (الأوسع)": cur["broad_ratio"],
                        "النسبة السابقة (الأحوط)": prev["strict_ratio"]})
        export[f"{i + 1}_{res.name[:20]}"] = ed

if len(results) > 1:
    st.subheader("مقارنة الملفات")
    sdf = pd.DataFrame(summary)
    st.dataframe(sdf.style.format({"إجمالي الإيرادات": "{:,.0f}", "غير المتوافق (الأحوط)": "{:,.0f}",
                                   "النسبة (الأحوط)": "{:.2%}", "النسبة (الأوسع)": "{:.2%}",
                                   "النسبة السابقة (الأحوط)": "{:.2%}"}), hide_index=True)

buf = io.BytesIO()
with pd.ExcelWriter(buf, engine="openpyxl") as xw:
    pd.DataFrame(summary).to_excel(xw, sheet_name="الملخص", index=False)
    for k, d in export.items():
        d.to_excel(xw, sheet_name=k.replace("/", "-")[:31], index=False)
st.download_button("تنزيل تقرير Excel", buf.getvalue(), "purification_report.xlsx")
