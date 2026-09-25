# Pipeline

1. O roteiro oficial vive em `episodios/CO-001/roteiro_narracao.md`.
2. `python main.py narracao` sintetiza cada beat pelo Azure Speech SDK.
3. Áudio e eventos `WordBoundary` da mesma síntese compartilham o `synthesis_id`.
4. Os timings alimentam `episodios/CO-001/scene_map.json`.
5. Cada cena receberá uma imagem ilustrada forte do FIN ou um apoio visual estritamente necessário.
6. Motion padrão: zoom lento, pan sutil, fade ou mask reveal simples.
7. O render final combinará imagem, áudio e timeline com `ffmpeg`.

Nesta fase, as etapas 1–4 estão preservadas. Geração de imagens e render final ainda não possuem entrypoint canônico e não devem ser iniciados implicitamente.
