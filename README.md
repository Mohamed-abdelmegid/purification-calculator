# حاسبة نسبة التطهير - البنوك الإسلامية

نسبة التطهير = الإيراد غير المتوافق ÷ إجمالي الإيرادات (قبل خصم تكلفة الودائع).

**قاعدة الأحوط:** أذون وسندات الخزانة وأدوات الدين الحكومية وعوائد الودائع والأرصدة لدى البنوك = غير متوافق.

## التشغيل محلياً
```
python -m pip install -r requirements.txt
python -m streamlit run app.py
```
OCR (للملفات الممسوحة) يحتاج برنامج Tesseract؛ بدونه تشتغل الأداة على الملفات النصية فقط.

## النشر على Streamlit Community Cloud
ارفع الملفات على GitHub (app.py, engine.py, requirements.txt, packages.txt) واختر app.py كملف رئيسي.
ملف packages.txt بيثبّت Tesseract (إنجليزي + عربي) أوتوماتيك.
