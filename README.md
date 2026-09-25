# Capital Oculto

Projeto editorial e audiovisual sobre psicologia do dinheiro e comportamento financeiro. A fase atual mantém um pipeline pequeno: roteiro, narração Azure com timing real, mapa de cenas ilustradas do FIN, motion simples e render final.

## Pipeline oficial

```text
ROTEIRO → NARRAÇÃO → WORD_BOUNDARY_REAL → SCENE MAP
        → IMAGEM ILUSTRADA DO FIN → MOTION SIMPLES → RENDER FINAL
```

Somente roteiro, narração, timings e scene map estão consolidados neste momento. A geração de imagens e o novo renderizador ainda não foram iniciados.

## Comandos

```powershell
python main.py narracao
python scripts/gerar_narracao.py --help
python scripts/validar_projeto.py
python -m unittest discover -s tests -p "test_*.py"
```

A narração usa `azure_sdk` por padrão. Configure `AZURE_SPEECH_KEY` e `AZURE_SPEECH_REGION` no ambiente ou em `scripts/.env`; esse arquivo não é versionado.

## Estrutura

- `config/`: provider, narradores, pacing, FIN e dependências aprovadas.
- `src/narration/`: engine e providers de voz.
- `episodios/CO-001/`: roteiro, pesquisa, scene map e timings oficiais.
- `assets/character_bible/`: fonte visual oficial do FIN.
- `scripts/`: entrypoints canônicos e validação.
- `tests/`: testes pequenos da funcionalidade ativa.
- `output/`: áudio, imagens, previews e renders gerados; ignorado pelo Git.

Novos backgrounds, props e imagens geradas devem ser adicionados a `assets/` apenas quando o pipeline ilustrado for configurado. A referência atual do personagem está em `assets/character_bible/fin.svg`.
