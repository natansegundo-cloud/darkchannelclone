# Capital Oculto

Projeto editorial e audiovisual sobre psicologia do dinheiro e comportamento financeiro. A fase atual mantém um pipeline pequeno: roteiro, narração Azure com timing real, mapa de cenas ilustradas do FIN, motion simples e render final.

## Pipeline oficial

```text
ROTEIRO → NARRAÇÃO → WORD_BOUNDARY_REAL → SCENE MAP
        → IMAGEM ILUSTRADA DO FIN → MOTION SIMPLES → RENDER FINAL
```

O episódio ativo vem de `config/project.json:active_episode`; seus arquivos vivem em `episodios/<active_episode>/`. A fase atual é `visual_qualification`, portanto `scene_map.json` e `visual_scenes.json` podem ser subconjuntos coerentes do `roteiro_visual.csv`, sem exigir narração completa nem quantidade fixa de cenas. Prompts são compilados em memória e não são persistidos em TXT. A integração permanece neutra de provider e usa `mock` até a conexão de uma API real.

A qualificação visual do episódio ativo está registrada como `visual_qualification.status=PASSED`. O próximo insumo de produção é a narração oficial; mudar para `production` continuará bloqueado enquanto identidade, cobertura dinâmica, timing real, assets aprovados e overlays não estiverem completos.

## Comandos

```powershell
python main.py narracao
python main.py narracao --official --dry-run
python main.py narracao benchmark-voices --beats B001,B005,B011,B015,B037 --voices pt-BR-AntonioNeural,pt-BR-FabioNeural --output-gain-db -3 --reuse-official-cache --dry-run
python main.py narracao benchmark-voices --beats B001,B005,B011,B015,B037 --voices pt-BR-HumbertoNeural --output-gain-variants-db=-7,-8,-9 --dry-run
python main.py narracao cleanup-humberto --beats B001,B005,B011,B015,B037 --dry-run
python main.py narracao benchmark-azure-48k --beats B001,B005,B037 --dry-run
python main.py visuals generate --scenes S004,S007,S009
python main.py visuals generate --all
python main.py visuals generate --all --budget 5.40 --enable-premium
python main.py visuals generate --scenes S004,S009 --dry-run
python main.py visuals validate
python main.py visuals benchmark --scene S004 --dry-run
python main.py visuals approve --scenes S010,S011
python main.py visuals upgrade --scenes S010,S012
python main.py render validate
python main.py render final --dry-run
# Comando real pago, executar somente após revisar o dry-run:
python main.py visuals generate --scenes S010,S011,S012,S013,S014,S015 --budget 0.15
python scripts/gerar_narracao.py --help
python scripts/validar_projeto.py
python -m unittest discover -s tests -p "test_*.py"
```

A fala oficial vem exclusivamente de `episodios/<active_episode>/roteiro_narracao.md`, onde cada trecho falado pertence a um beat explícito. `python main.py narracao --official --dry-run` valida o documento e grava `narration_plan.json` sem chamar Azure nem gerar WAV. B001–B013 permanecem no código apenas como `pilot_beats` compatíveis com testes antigos; eles não são usados no modo oficial. `config/motion_contract.json:voice_pacing` é a autoridade operacional de voice, rate, pitch, pausas e densidade. Configure `AZURE_SPEECH_KEY` e `AZURE_SPEECH_REGION` no ambiente ou em `scripts/.env`; esse arquivo não é versionado.

`narracao benchmark-voices` compara vozes em uma saída isolada sob `output/audio/<episode_id>/voice_benchmark/<run_id>/`. O `--dry-run` nunca chama Azure; `--reuse-official-cache` reutiliza Antonio somente após validar texto, hashes, delivery, voz, pacing, WAV e `WORD_BOUNDARY_REAL`. O ganho final do benchmark não altera a masterização nem os artefatos oficiais.

Para diagnosticar volume sem ressintetizar, `--output-gain-variants-db` deriva múltiplas masters locais do mesmo raw e registra clipping, pico, RMS e reduções aproximadas de compressor/limiter. O raw candidato fica dentro do próprio benchmark e pode ser reutilizado em execuções posteriores após validação integral do cache.

`narracao cleanup-humberto` usa somente o cache raw Humberto e produz quatro variantes locais a `-9 dB`: baseline, denoise conservador via `afftdn`, low-pass de 10,5 kHz e a combinação dos dois. O manifest compara noise floor e energia de alta frequência sem alterar WordBoundary, síntese ou artefatos oficiais.

`narracao benchmark-azure-48k` compara Humberto, Donato e Valerio em `Riff48Khz16BitMonoPcm`. Os WAVs individuais são os bytes raw retornados pelo Azure SDK, com WordBoundary da mesma síntese; apenas `comparison.wav` é montado localmente com 700 ms de silêncio entre beats, sem mastering ou resampling.

O benchmark OpenRouter usa exclusivamente `OPENROUTER_API_KEY` do ambiente; `scripts/.env` pode inicializar essa variável localmente sem versionar ou registrar o segredo. Ele compara quatro candidatos sem fallback e aplica teto total de US$ 0,15.

Cada POST real é registrado em `request_audit.jsonl` antes do envio e vinculado ao `request_id`/`usage.cost` da resposta. Uma rodada real existente ou concorrente bloqueia nova execução; rerun exige `--allow-rerun --reason MOTIVO`.

## Estrutura

- `config/`: provider, narradores, pacing, FIN, tiers de geração e perfil visual aprovado.
- `src/narration/`: engine e providers de voz.
- `src/visuals/`: compilação de prompts em memória, providers, orçamento, fallback, runner e validadores.
- `src/render/`: preflight fail-closed, plano determinístico e montagem FFmpeg do vídeo final.
- `episodios/<active_episode>/`: metadados de fase, roteiro, pesquisa, timings disponíveis, scene map e `visual_scenes.json`.
- `assets/character_bible/`: fonte visual oficial do FIN.
- `scripts/`: entrypoints canônicos e validação.
- `tests/`: testes pequenos da funcionalidade ativa.
- `output/generated_images/`: imagens, `manifest.json` e `summary.md`; ignorado pelo Git.
- `output/render/<active_episode>/`: plano, manifest e MP4 final após render bem-sucedido; ignorado pelo Git.

O lote visual usa `output/generated_images/drafts/` para FLUX, `output/generated_images/final/` para upgrades GPT e `review_index.html` para revisão local. A aprovação não chama API; o upgrade exige `review_status=UPGRADE_REQUESTED` e chama diretamente GPT Image 2.

Novos backgrounds, props e imagens geradas devem ser adicionados a `assets/` apenas quando houver aprovação explícita. As referências oficiais do FIN são `assets/character_bible/fin_turnaround.png` e `assets/character_bible/fin_poses.png`; o SVG é legado.

O render final permanece bloqueado enquanto `production_stage=visual_qualification`. `render validate` executa apenas o preflight e nunca chama FFmpeg. Quando o episódio estiver em `production`, `render final --dry-run` exigirá narração oficial, `WORD_BOUNDARY_REAL`, timeline contínua, assets aprovados existentes, motions suportados e ausência de overlays pendentes; ele grava o plano, mas não produz vídeo. O render real usa 1920x1080, 30 fps, H.264 e AAC.
