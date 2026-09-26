# FIN

FIN é o protagonista visual do cotidiano financeiro e o `audience proxy` do Capital Oculto. Ele representa o espectador vivendo a situação narrada, não apenas explicando ou apontando para conceitos.

FIN pode aparecer trabalhando, comprando, esperando, pensando, pagando, conferindo o celular, olhando um recibo, organizando contas, comparando escolhas e percebendo mudanças de hábito. A ação, a expressão e o ambiente devem parecer parte da vida real. Poses frontais de apresentador são exceção, não padrão.

Cada imagem com o personagem deve mostrar apenas um FIN e nenhuma pessoa adicional, salvo futura decisão editorial explícita. O quadro deve parecer um momento cotidiano capturado, não uma vitrine de pose do personagem.

## Lock visual

O lock oficial permanece `FIN_V1`, definido em `config/character_fin.json`. As referências canônicas são:

- `assets/character_bible/fin_turnaround.png` para identidade, proporções e ângulos;
- `assets/character_bible/fin_poses.png` para poses, expressões e gestos.

São invariáveis: cabeça, rosto, cabelo, roupa, gravata, proporções, mãos, pernas, sapatos, paleta, contorno e estilo de ilustração. `fin.svg` é legado técnico e não orienta novas imagens.

`config/character_fin.json` é a única fonte do lock completo. Para evitar prompts excessivamente longos, o builder pode emitir um resumo canônico contendo obrigatoriamente:

- `FIN_V1`;
- as duas referências oficiais;
- proibição explícita de redesign;
- rosto e cabelo essenciais;
- terno preto, camisa branca e gravata lima;
- proporções compactas com cabeça ampliada;
- paleta e contorno oficiais;
- caminho do lock completo.

O resumo serve apenas para transportar a identidade ao gerador. Qualquer conflito é resolvido a favor do arquivo canônico e das folhas de referência.

Por enquanto, não criar nova wardrobe bible, roupas alternativas ou variações de identidade. O mesmo FIN deve continuar reconhecível em todos os ambientes, enquadramentos e ações.
