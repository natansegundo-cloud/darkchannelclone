# Dependências aprovadas

| Tipo | Dependência | Versão | Uso aprovado |
|---|---|---:|---|
| Python | `azure-cognitiveservices-speech` | `1.51.2` | Narração oficial e eventos reais de `WordBoundary`. |
| Ferramenta externa | `ffmpeg` | versão do ambiente | Composição de imagem e áudio, motion simples, concatenação e export final quando o renderizador ilustrado for implementado. |
| Serviço externo | `OpenRouter Image API` | `/api/v1/images` | Benchmark controlado de modelos de imagem; autenticação somente por `OPENROUTER_API_KEY`. |

O caminho canônico é `azure_sdk`, com `timing_quality=WORD_BOUNDARY_REAL`. O provider local Kokoro existe apenas como fallback explícito; não há fallback silencioso.
