# Pipeline

```text
ROTEIRO OFICIAL
  → NARRAÇÃO AZURE + WORD_BOUNDARY_REAL
  → VISUAL_SCENES.JSON
  → PROMPT COMPILADO EM MEMÓRIA
  → CHEAP_DRAFT → MID_FALLBACK → OPTIONAL_PREMIUM
  → VALIDAÇÃO + REVISÃO HUMANA
  → IMAGEM ESTÁTICA 16:9
  → MOTION SIMPLES
  → RENDER
```

## Narração e timing

O roteiro oficial permanece em `episodios/CO-001/roteiro_narracao.md`. `python main.py narracao` sintetiza cada beat pelo Azure Speech SDK, mantendo áudio e eventos `WordBoundary` ligados pelo mesmo `synthesis_id`. Os timings oficiais alimentam `episodios/CO-001/scene_map.json` e não são recalculados pela camada visual.

## Planejamento visual

`episodios/CO-001/visual_scenes.json` é o source of truth de S001–S018 e contém somente dados variáveis de cada cena. `src/visuals/engine.py` valida e seleciona esses objetos. `src/visuals/prompt_builder.py` resolve `FIN_V1`, `ILLUSTRATED_V1` e as regras globais, compilando uma única linha autocontida em memória para o provider.

O mesmo JSON contém a `text_policy` obrigatória de cada cena. Os únicos modos de produção são `NONE` e `OVERLAY`. Em `OVERLAY`, o conteúdo textual permanece apenas no metadata estruturado; o modelo recebe somente os alvos de superfícies limpas para aplicação determinística posterior. A regra global é `NO UNDECLARED TEXT`: o modelo nunca decide sozinho se deve inserir letras, números, labels ou pseudo-texto. `EXACT` não é aceito em produção.

A fronteira oficial é `SYSTEM DECIDES CONTENT / AI DECIDES APPEARANCE`. Ideia dominante, situação, ação, expressão, ambiente, props, framing, iluminação, continuidade, identidade do FIN e política textual chegam ao modelo como decisões obrigatórias. O modelo escolhe apenas detalhes finos de renderização que não alterem a leitura narrativa.

Prompt é artefato compilado, não input. TXT de prompt não é canônico e não é salvo automaticamente. `build_source_prompt(scene)` existe apenas para debug ou revisão humana em memória.

O lock completo do personagem é carregado de `config/character_fin.json`. O prompt final usa uma referência resumida a `FIN_V1`, às duas imagens oficiais, aos traços essenciais e à proibição de redesign. Isso reduz repetição sem criar uma segunda fonte de identidade.

## Geração em três níveis

`config/visual_generation.json` define provider, modelo, tentativas, timeout e custo estimado dos tiers `cheap_draft`, `mid_fallback` e `premium_optional`. `QUALITY_FIRST` permanece ativo: aderência narrativa, consistência `FIN_V1`, qualidade visual e continuidade vêm antes de custo, e upgrade por qualidade nunca é automático. O runner tenta cada cena primeiro no nível barato, avança ao intermediário somente após falha técnica prevista e usa premium apenas quando habilitado. `BudgetManager` monitora o custo e respeita o valor autorizado para a execução; esse controle nunca transforma uma imagem ruim em aprovada.

`config/visual_reference_profile.json` trava `ILLUSTRATED_V1`, `FIN_V1`, as duas imagens do character bible e os frames qualitativos aprovados de S004, S007 e S009. Esses frames orientam linguagem visual; não são templates de geometria reutilizável.

O contrato de provider está em `src/visuals/providers.py`. O provider `mock` é o padrão offline atual: não chama API e grava um PNG simulado para exercitar jobs, fallback, orçamento, manifest e validação. Uma integração real deve implementar o mesmo contrato, sem alterar engine, prompts ou estrutura de saída.

O comando `python main.py visuals benchmark --scene S004 --dry-run` prepara a comparação OpenRouter de S004 sem chamada externa. O benchmark real é independente dos tiers de produção, usa uma chamada por candidato, não aplica fallback e para novas chamadas ao atingir US$ 0,15.

Requests reais recebem sequência, tentativa, motivo, timestamp e endpoint em `request_audit.jsonl`. O custo e o request id vêm da mesma resposta que entrega `b64_json`. Lock de processo e bloqueio de rerun evitam duas rodadas acidentais no mesmo output.

Cada execução grava diretamente em `output/generated_images/`: `S001.png`, `S002.png` etc., além de `manifest.json` e `summary.md`. O manifest guarda metadados e `prompt_hash`, nunca o prompt completo. Use:

```powershell
python main.py visuals generate --scenes S004,S007,S009
python main.py visuals generate --all --budget 5.40
python main.py visuals generate --all --enable-premium
python main.py visuals generate --scenes S004,S009 --dry-run
python main.py visuals validate
```

## Imagem primeiro, motion depois

A imagem estática precisa funcionar sozinha antes de qualquer animação. Após aprovação, usar apenas motion leve e motivado: zoom in/out lento, pan sutil, hold, crossfade, parallax discreto ou shake curto. O render final combinará imagem aprovada, narração e timeline; não faz parte da etapa textual atual.

A imagem-base deve ser limpa, situacional e legível antes da animação. A prioridade é situação, ação, legibilidade, FIN subordinado à cena e somente então o cenário mínimo necessário. A imagem deve parecer um instante capturado da vida cotidiana, nunca uma pose de catálogo. A revisão humana acontece antes de qualquer motion simples.
