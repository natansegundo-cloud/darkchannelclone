# FIN — character bible

`fin.svg` é a fonte visual oficial do protagonista recorrente do Capital Oculto.

## Estrutura obrigatória

Somente `char-base` é um `<symbol>`. Todas as partes vivem como `<g>` no mesmo sistema de coordenadas `320x640`:

- `char-head`;
- `char-neck`;
- `char-torso`;
- `char-tie`;
- `char-arm-left`;
- `char-arm-right`;
- `char-leg-left`;
- `char-leg-right`.

É proibido converter qualquer uma dessas partes para `<symbol>`. Isso recriaria o aninhamento de viewports que causou os bugs críticos das versões anteriores.

## Uso

- Cena estática: uma única instância `<use href="#char-base">`.
- Cena articulada: montar as partes com `<use href="#char-head">`, `<use href="#char-arm-left">` e equivalentes.
- Nunca copiar ou redesenhar a geometria do personagem dentro de uma cena.
- Nunca alterar proporções, rosto, gravata, espessuras, silhueta-base ou paleta.

## Pivôs

Os pivôs estão registrados em `data-pivot` no próprio SVG:

- cabeça: `160 205`;
- torso e gravata: `160 225`;
- braço esquerdo: `112 231`;
- braço direito: `208 231`;
- perna esquerda: `137 370`;
- perna direita: `183 370`.

## Paleta imutável

- preto: `#111111`;
- lima: `#C4E538`;
- off-white: `#F4F3EF`.

Qualquer evolução deve alterar o arquivo canônico existente e passar por revisão visual. Não crie cópias com sufixos de versão.
