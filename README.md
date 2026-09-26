# Capital Oculto

Projeto editorial e audiovisual sobre psicologia do dinheiro e comportamento financeiro. A fase atual mantém um pipeline pequeno: roteiro, narração Azure com timing real, mapa de cenas ilustradas do FIN, motion simples e render final.

## Pipeline oficial

```text
ROTEIRO → NARRAÇÃO → WORD_BOUNDARY_REAL → SCENE MAP
        → IMAGEM ILUSTRADA DO FIN → MOTION SIMPLES → RENDER FINAL
```

Roteiro, narração, timings, scene map e o pipeline visual em três níveis estão ativos. `episodios/CO-001/visual_scenes.json` é a fonte canônica das cenas e da `text_policy`; prompts são compilados em memória e não são persistidos em TXT. A integração permanece neutra de provider e usa `mock` até a conexão de uma API real.

## Comandos

```powershell
python main.py narracao
python main.py visuals generate --scenes S004,S007,S009
python main.py visuals generate --all
python main.py visuals generate --all --budget 5.40 --enable-premium
python main.py visuals generate --scenes S004,S009 --dry-run
python main.py visuals validate
python main.py visuals benchmark --scene S004 --dry-run
python main.py visuals approve --scenes S010,S011
python main.py visuals upgrade --scenes S010,S012
# Comando real pago, executar somente após revisar o dry-run:
python main.py visuals generate --scenes S010,S011,S012,S013,S014,S015 --budget 0.15
python scripts/gerar_narracao.py --help
python scripts/validar_projeto.py
python -m unittest discover -s tests -p "test_*.py"
```

A narração usa `azure_sdk` por padrão. Configure `AZURE_SPEECH_KEY` e `AZURE_SPEECH_REGION` no ambiente ou em `scripts/.env`; esse arquivo não é versionado.

O benchmark OpenRouter usa exclusivamente `OPENROUTER_API_KEY` do ambiente; `scripts/.env` pode inicializar essa variável localmente sem versionar ou registrar o segredo. Ele compara quatro candidatos sem fallback e aplica teto total de US$ 0,15.

Cada POST real é registrado em `request_audit.jsonl` antes do envio e vinculado ao `request_id`/`usage.cost` da resposta. Uma rodada real existente ou concorrente bloqueia nova execução; rerun exige `--allow-rerun --reason MOTIVO`.

## Estrutura

- `config/`: provider, narradores, pacing, FIN, tiers de geração e perfil visual aprovado.
- `src/narration/`: engine e providers de voz.
- `src/visuals/`: compilação de prompts em memória, providers, orçamento, fallback, runner e validadores.
- `episodios/CO-001/`: roteiro, pesquisa, timings, scene map e `visual_scenes.json` oficiais.
- `assets/character_bible/`: fonte visual oficial do FIN.
- `scripts/`: entrypoints canônicos e validação.
- `tests/`: testes pequenos da funcionalidade ativa.
- `output/generated_images/`: imagens, `manifest.json` e `summary.md`; ignorado pelo Git.

O lote visual usa `output/generated_images/drafts/` para FLUX, `output/generated_images/final/` para upgrades GPT e `review_index.html` para revisão local. A aprovação não chama API; o upgrade exige `review_status=UPGRADE_REQUESTED` e chama diretamente GPT Image 2.

Novos backgrounds, props e imagens geradas devem ser adicionados a `assets/` apenas quando houver aprovação explícita. As referências oficiais do FIN são `assets/character_bible/fin_turnaround.png` e `assets/character_bible/fin_poses.png`; o SVG é legado.
