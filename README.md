# DAYLUXE AI CEO

نظام تشغيل ذكي لشركة **Dayluxe Roofing & Property Services** — ينفّذ الـ Master System Prompt بالكامل:
مدير (CEO) ينسّق وكلاء متخصصين للمبيعات، الإيميل، التسويق، الصور، الموقع/SEO، عروض الأسعار، دعم الزبائن، وتطوير الأعمال.

- **بدون أي مكتبات خارجية** — Python 3.12+ فقط (ماكو `pip install` ولا أخطاء تثبيت).
- **29 اختبار** تغطي كل الأجزاء، كلها ناجحة: `python -m unittest discover -s tests -t .`
- **الأمان أولاً**: كل إيميل، منشور، عرض سعر، أو نشر صفحة يروح لك للموافقة قبل التنفيذ (الإعدادات الافتراضية).

## شنو يسوي النظام

| الوكيل | الشغل |
|---|---|
| CEO | كل دورة: يفحص صحة الخدمات ← يجيب الإيميلات والـ leads ← يرتب الأولويات ← يوزع الشغل ← يكتب تقرير. إذا خدمة وحدة خربت، الباقي يكمل. |
| Sales | يستخرج الخدمة، المنطقة، الاستعجال، الرقم؛ يعطي تقييم 0–100؛ يمنع التكرار (نفس الإيميل/الرقم = نفس الزبون)؛ ملخص لك بالعراقي. |
| Email | يقرا Gmail، يصنّف (استفسار / hipages وغيرها / شكوى / مورّد / سبام)، يكتب رد بالإنكليزي الأسترالي ويحطه Draft بـ Gmail + بانتظار موافقتك. |
| Marketing | جدول أسبوعي لمنشورات فيسبوك/إنستغرام حسب الموسم بسدني + خطة إعلان شهرية للمراجعة (ما يصرف ولا دولار). |
| Images | صور تسويقية (اختياري) مع backoff، وإذا خلص الرصيد يوقف وما يعيد المحاولة. |
| Website/SEO | يراجع صفحات WordPress (عناوين طويلة/مكررة، محتوى قليل، بدون CTA) ويكتب صفحة منطقة وحدة كل مرة كـ Draft. |
| Quote | عرض سعر من **أسعارك أنت** فقط (`config/pricing.json`) + القياسات اللي تدخلها. أي شي ناقص = ما ينرسل. |
| Support | يكشف الشكاوى، السلامة، النزاعات المالية، والقانونية ويرفعها لك بدون رد تلقائي. |
| BizDev | إيميلات تعريفية لوكالات العقار والـ strata اللي تضيفهم أنت، مع رابط إلغاء اشتراك (Spam Act). ما ينرسل تلقائياً أبداً. |

## التشغيل على جهازك

```bash
cp .env.example .env              # عبّي ADMIN_TOKEN و WEBHOOK_SECRET على الأقل
cp config/pricing.example.json config/pricing.json   # حط أسعارك
python -m dayluxe check-config    # يفحص الإعدادات
python -m dayluxe health          # يفحص الاتصال بكل خدمة
python -m dayluxe run             # دورة وحدة ويطبع التقرير
python -m dayluxe serve           # لوحة التحكم على http://localhost:8000
```

لتوليد توكن قوي: `python -c "import secrets;print(secrets.token_urlsafe(32))"`

## النشر على Render

1. ارفع المجلد على GitHub (ملف `.gitignore` يمنع رفع `.env` والبيانات).
2. Render ← **New → Blueprint** ← اختار الـ repo. ملف `render.yaml` ينشئ خدمة ويب وتوكنات عشوائية. الخطة المجانية ما بيها قرص دائم، لذلك قاعدة البيانات ممكن تنمسح بعد restart أو deploy؛ إذا تريد حفظ دائم، تحتاج خطة مدفوعة وقرص دائم.
3. بالـ Environment عبّي المفاتيح اللي عندك (Gmail، WordPress، Meta، OpenAI أو Gemini).
4. `ADMIN_TOKEN` المولّد تلقائياً هو كلمة سر الدخول للوحة.
5. ارفع أسعارك: انسخ محتوى `pricing.example.json` إلى `/var/data/pricing.json` (من Render Shell).

> القرص الدائم يحتاج خطة مدفوعة (Starter). بدونه البيانات تنمسح مع كل deploy.

## ربط الخدمات

**Gmail**: Google Cloud Console ← OAuth Client (Desktop/Web) ← فعّل Gmail API ← خذ refresh token بالصلاحيات
`gmail.readonly` و `gmail.compose` (مثلاً من OAuth Playground). حط `GOOGLE_CLIENT_ID / SECRET / REFRESH_TOKEN`.

**WordPress**: لوحة WordPress ← Users ← Profile ← Application Passwords ← أنشئ واحد. حط `WP_USERNAME` و `WP_APP_PASSWORD`.
(لازم الموقع يقبل REST API؛ بعض إضافات الحماية بـ Hostinger تقفله.)

**فورم الموقع** (WPForms / Contact Form 7 / Elementor عبر webhook):
- الرابط: `https://<your-app>.onrender.com/webhook/lead`
- Header: `X-Webhook-Secret: <WEBHOOK_SECRET>`
- الحقول المقبولة: name, email, phone, suburb, service, message (يفهم أسماء CF7 مثل `your-name` تلقائياً).

**Facebook/Instagram**: Page access token دائم بصلاحية `pages_manage_posts` (و `instagram_content_publish` لإنستغرام)
+ `META_PAGE_ID` و `META_IG_USER_ID`. إنستغرام يحتاج صورة برابط عام، فحط `PUBLIC_BASE_URL` ووفعّل `GENERATE_IMAGES` أو انشر صورك يدوياً.

**الذكاء الاصطناعي**: `OPENAI_API_KEY` أو `GEMINI_API_KEY`. بدونهم النظام يشتغل بقوالب جاهزة مكتوبة بأسلوب طبيعي.
أسماء الموديلات قابلة للتغيير من `.env` (`OPENAI_TEXT_MODEL`, `GEMINI_TEXT_MODEL`, `OPENAI_IMAGE_MODEL`).

## سياسة الموافقات

```
AUTO_SEND_EMAIL=false  AUTO_PUBLISH_SOCIAL=false  AUTO_SPEND_AD_BUDGET=false
AUTO_SEND_QUOTES=false CEO_APPROVAL_REQUIRED=true
```
- الشي يتنفذ تلقائياً **فقط** إذا مفتاح `AUTO_*` الخاص بيه `true` **و** `CEO_APPROVAL_REQUIRED=false`.
- الإعلانات، الإيميلات التعريفية، نشر صفحات الموقع، والشكاوى: **دائماً** تحتاج موافقتك مهما كانت الإعدادات.
- كل موافقة تتسجل: منو وافق، متى، وهل التنفيذ نجح فعلاً (النظام ما يقول "تم" إلا إذا الخدمة أكدت).
- تكدر تعدل نص الإيميل أو المنشور باللوحة قبل ما توافق.

## API (مع `Authorization: Bearer <ADMIN_TOKEN>`)

| Method | Path | الوظيفة |
|---|---|---|
| GET | `/api/status` | الصحة + الأرقام + الأولويات + آخر دورة |
| POST | `/api/run` | تشغيل دورة الآن |
| GET | `/api/leads`, `/api/leads/{id}` | الزبائن وتاريخ استفساراتهم |
| POST | `/api/leads/{id}/status` | `{"status":"won"}` (new/contacted/quoted/won/lost/spam) |
| GET | `/api/approvals?status=pending\|all` | قائمة الموافقات |
| POST | `/api/approvals/{id}/approve` أو `/reject` | قرار (اختياري `{"edits":{"body":"..."}}`) |
| POST | `/api/quotes` | `{"lead_id":1,"lines":[{"item":"gutter_guard","quantity":40}],"scope_notes":"..."}` |
| POST | `/api/prospects` | `{"organisation":"...","email":"...","category":"strata","consent_basis":"..."}` |
| POST | `/webhook/lead` | فورم الموقع (بالـ secret) |
| GET | `/health` | فحص Render |

## هيكل المشروع

```
dayluxe/
  config.py        الإعدادات + القيم الآمنة الافتراضية
  storage.py       SQLite: الزبائن، الاستفسارات، الموافقات، السجل، المحتوى
  http_client.py   مهلات + backoff + تصنيف الأخطاء (بدون كشف أسرار)
  llm.py           OpenAI / Gemini مع بديل جاهز
  approvals.py     سياسة الموافقات والتنفيذ
  ceo.py           دورة التشغيل والتقرير
  web.py           اللوحة + API + webhook + المجدول
  integrations/    gmail, wordpress, meta, images
  agents/          sales, email_agent, intake, marketing, seo, quote, support, bizdev
tests/test_system.py
```
