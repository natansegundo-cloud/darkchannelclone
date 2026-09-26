# AGENTS.md — Capital Oculto

## Caminhos oficiais

- Narração: `src/narration/` e `scripts/gerar_narracao.py`.
- Configuração: `config/`.
- Episódio ativo: `episodios/CO-001/`.
- Cenas visuais canônicas: `episodios/CO-001/visual_scenes.json`.
- Identidade do FIN: `assets/character_bible/`.
- Saídas reproduzíveis: `output/` (não versionado).

## Regras

1. Não procure implementações históricas nem consulte `_archive/` em tarefas normais.
2. Não leia o repositório inteiro para resolver uma tarefa localizada; comece pelo entrypoint do domínio.
3. Narração oficial usa `azure_sdk` e `WORD_BOUNDARY_REAL`; fallbacks são sempre explícitos.
4. Preserve a identidade, proporções e paleta do FIN conforme a character bible.
5. Priorize uma imagem estática forte antes de adicionar motion.
6. Use por padrão zoom lento, pan sutil, fade ou mask reveal simples; morph é exceção.
7. Dependências externas só podem ser usadas após registro em `config/dependencies.md`.
8. Não crie versões paralelas de arquivos. São proibidos os sufixos `_v2`, `_v3`, `_final`, `_novo`, `_refined` e `_candidate`; use Git.
9. Não gere imagens nem vídeo sem solicitação explícita.
10. Antes de concluir alterações, execute `python scripts/validar_projeto.py` e os testes relevantes.
11. JSON é source of truth visual; prompts são compilados em memória e arquivos TXT de prompt não são canônicos.
