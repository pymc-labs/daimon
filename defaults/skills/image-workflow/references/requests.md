# Image requests

Generation:

```json
{"operation":"generate","provider":"openai","model":"gpt-image-2","prompt":"A red cube on a neutral background","size":"1024x1024","quality":"medium"}
```

Editing:

```json
{"operation":"edit","provider":"openai","prompt":"Make the cube blue, preserving its placement and lighting","reference":"/mnt/session/uploads/reference.png"}
```

OpenAI size: `1024x1024` (default), `1024x1536` or `1536x1024`. Quality: `low`,
`medium` (default), `high`. Models: `gpt-image-2` (default), `gpt-image-1.5`.
Count is fixed at one and output format at PNG. The Images edit endpoint receives
a local reference encoded as a data URL in the JSON `images` array; no file upload
or public hosting is required. PNG, JPEG and WebP references are accepted.

Google has retired Imagen in the Gemini API. Gemini requests are refused; no
GEMINI_API_KEY is needed. No provider URL output is fetched: OpenAI responses
must include image bytes as base64.

Official schemas: [OpenAI generate](https://developers.openai.com/api/reference/resources/images/methods/generate),
[OpenAI edit](https://developers.openai.com/api/reference/resources/images/methods/edit).
[Google's Imagen retirement notice](https://ai.google.dev/gemini-api/docs/imagen)
explains why the legacy optional route is unavailable.
Vendor access/model availability and real generation quality require acceptance
in the provisioned Preview provider project; offline tests prove neither.
