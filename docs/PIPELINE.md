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

O episódio ativo é resolvido uma única vez por `config/project.json:active_episode`. Seus arquivos ficam em `episodios/<active_episode>/`, e `episodio.json:production_stage` distingue `visual_qualification` de `production`. Nesta etapa, B001–B013 são metadata `pilot_beats`, com cobertura parcial, e não são rotulados como roteiro completo. A narração oficial ainda não foi implementada nem é exigida. Quando executado explicitamente, o piloto usa Azure Speech SDK e mantém áudio e eventos `WordBoundary` ligados pelo mesmo `synthesis_id`; timings reais existentes não são recalculados pela camada visual.

`config/motion_contract.json:voice_pacing` é a fonte operacional de voice, rate, pitch, faixas de pausa e speech density. `narrators.json` preserva identidade, provider, idioma, formato, credenciais e fallback; campos de delivery duplicados são validados como espelho exato do motion contract.

## Planejamento visual

`roteiro_visual.csv` define dinamicamente a sequência planejada do episódio, sem quantidade ou duração fixa. `visual_scenes.json` contém somente os dados variáveis das cenas selecionadas; durante `visual_qualification`, ele e `scene_map.json` podem representar subconjuntos coerentes do roteiro. Em `production`, a cobertura é comparada à sequência real declarada no roteiro, nunca a uma contagem hardcoded. `src/visuals/engine.py` valida e seleciona esses objetos. `src/visuals/prompt_builder.py` resolve `FIN_V1`, `ILLUSTRATED_V1` e as regras globais, compilando uma única linha autocontida em memória para o provider.

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

A imagem estática precisa funcionar sozinha antes de qualquer animação. Após aprovação, usar apenas motion leve e motivado: zoom in/out lento, pan sutil, hold, crossfade, parallax discreto ou shake curto. O render final combina imagem aprovada, narração oficial e timeline real por meio do domínio `src/render/`.

A imagem-base deve ser limpa, situacional e legível antes da animação. A prioridade é situação, ação, legibilidade, FIN subordinado à cena e somente então o cenário mínimo necessário. A imagem deve parecer um instante capturado da vida cotidiana, nunca uma pose de catálogo. A revisão humana acontece antes de qualquer motion simples.

## Preflight e render final

`python main.py render validate` executa somente o preflight. Ele resolve o episódio ativo dinamicamente e verifica narração oficial, timing `WORD_BOUNDARY_REAL` da mesma síntese, `scene_map.json`, cobertura visual, aprovação e existência dos assets, presets de motion, continuidade temporal e coerência de duração. O comando não chama FFmpeg nem produz vídeo.

`python main.py render final --dry-run` passa pelas mesmas travas, verifica a disponibilidade do FFmpeg, grava `output/render/<episode_id>/render_plan.json` e `render_manifest.json` e exibe a lista de argumentos planejada sem executar o render. O plano é derivado da quantidade real de cenas e permanece determinístico para os mesmos inputs.

`python main.py render final` só é permitido em `production`. Durante `visual_qualification`, ele encerra com `FINAL_RENDER_BLOCKED: episode is still in visual_qualification`, antes de invocar FFmpeg. O render real usa subprocess com lista de argumentos, sem shell, entrega 1920x1080 no frame rate central de 30 fps, H.264 e AAC, e publica `output/render/<episode_id>/final.mp4` somente após sucesso. Qualquer cena `OVERLAY` bloqueia enquanto o compositor determinístico de texto não existir.
