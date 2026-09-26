# Direção visual oficial

## Princípio editorial

O Capital Oculto usa ilustração situacional. O visual responde primeiro à pergunta: **“o que o espectador estaria vendo se essa situação estivesse acontecendo na vida real?”** A teoria permanece na narração; a imagem conta uma mini-história concreta.

A fronteira oficial é **SYSTEM DECIDES CONTENT / AI DECIDES APPEARANCE**. O JSON decide integralmente o conteúdo narrativo; o modelo decide somente o acabamento visual dentro dessas decisões. Ele não pode enriquecer, simplificar ou reinterpretar a cena adicionando ou removendo elementos narrativos.

FIN é o `audience proxy`: o protagonista visual que representa o público vivendo situações financeiras comuns. Ele não é apenas mascote, narrador ou apresentador em pose. FIN trabalha, compra, espera, paga, confere o celular, olha recibos, toma decisões e reage às consequências no ambiente em que elas acontecem.

## Hierarquia visual

Cada quadro prioriza, nesta ordem:

1. situação concreta;
2. ação do personagem;
3. contexto do ambiente;
4. props relevantes;
5. clareza imediata da leitura;
6. estilo e acentos visuais.

Uma cena deve comunicar uma ideia principal. O ambiente participa da narrativa sem virar decoração excessiva.

## Formato oficial do prompt

Todo prompt de `situational illustrated scene` segue exatamente esta ordem:

1. `SCENE TYPE`;
2. `CONCRETE SITUATION`;
3. `ACTION`;
4. `EXPRESSION`;
5. `ENVIRONMENT`;
6. `PROPS`;
7. `FRAMING`;
8. `SUBJECT COUNT`;
9. `CHARACTER CONSISTENCY`;
10. `LIGHTING`;
11. `STYLE`;
12. `CONTINUITY`;
13. `VISUAL REFERENCE`;
14. `VISUAL RULESET`;
15. `SCENE SIMPLIFICATION RULES`;
16. `BEHAVIOR RULE`;
17. `COMPOSITION PRIORITY`;
18. `READABILITY RULE`;
19. `TEXT POLICY`;
20. `CHARACTER EMPHASIS RULE`;
21. `BACKGROUND SUBORDINATION RULE`;
22. `PROP LIMIT RULE`;
23. `ANTI-STAGING RULE`;
24. `CAMERA SIMPLICITY RULE`;
25. `NEGATIVE RULES`.

Para `CHARACTER_SCENE` e `ENVIRONMENT_SCENE`, os cinco campos adicionais subordinam FIN e o cenário à ação: `CHARACTER EMPHASIS RULE`, `BACKGROUND SUBORDINATION RULE`, `PROP LIMIT RULE`, `ANTI-STAGING RULE` e `CAMERA SIMPLICITY RULE`. Eles aparecem uma única vez em cada formato. Para cenas com FIN, `SUBJECT COUNT` continua sendo `ONE FIN only. No additional people.`

O builder compila esses campos em uma única linha contínua no formato `CAMPO: conteúdo | CAMPO: conteúdo`. Essa string existe somente em memória e segue diretamente para o provider. `build_source_prompt(scene)` oferece uma visualização multilinha apenas para debug; TXT de prompt não é fonte canônica nem output automático.

`VISUAL REFERENCE` aplica `ILLUSTRATED_V1` como linguagem qualitativa canônica. As referências de S004, S007 e S009 definem naturalidade, economia visual, ação dominante e integração do FIN; não congelam enquadramento, posição ou geometria para outras cenas.

As imagens do character bible são referências de personagem e só entram no request quando `character_presence` é `FIN`. Cenas `NONE` não recebem `fin_turnaround.png` nem `fin_poses.png`. Geração normal, upgrade e benchmark usam a mesma resolução centralizada; uma referência específica em cena `NONE` exige compatibilidade `NONE` explícita no perfil.

## Política de texto visual

O `visual_scenes.json` do episódio ativo é a fonte de verdade para qualquer texto ou número visível. O gerador nunca decide sozinho que uma cena precisa de texto: toda ocorrência deve ser coberta por `text_policy`.

`text_policy` usa exatamente um destes modos:

- `NONE`: nenhum texto, letra, número, rótulo, pseudo-palavra, gibberish ou tipografia decorativa;
- `OVERLAY`: não renderizar o conteúdo declarado; gerar apenas a superfície limpa reservada para overlay posterior.

A regra global é `NO UNDECLARED TEXT`. `EXACT` não é modo oficial de produção. O builder mantém os textos declarados no metadata estruturado, compila somente os alvos de superfícies limpas e preserva o prompt single-line. Texto de narração, CSV legado e intenção implícita não autorizam texto na imagem.

## Ruleset rígido para FIN

Toda `CHARACTER_SCENE` ou `ENVIRONMENT_SCENE` com FIN obedece às seguintes travas:

- uma cena comunica uma ideia principal e uma ação principal;
- o quadro parece um momento cotidiano capturado, nunca uma pose promocional;
- a situação é o foco; FIN serve à situação;
- o ambiente contextualiza sem competir;
- usar no máximo três props narrativamente essenciais;
- fundo simples, limpo e subordinado à ação;
- nenhum objeto decorativo sem função, texto não declarado, infográfico ou composição abstrata;
- FIN ocupa normalmente 25% a 40% do quadro; close narrativamente justificado é exceção;
- pose natural e funcional, expressão ligada à situação;
- a ação deve ser compreendida em até um segundo.

## Simplificação concreta

- Compra cotidiana: uma sacola modesta, um recibo e um único sinal de pagamento ou balcão.
- Adaptação ou rotina: um objeto recorrente, um gesto claro e ambiente doméstico simples.
- Segurança material: escolher no máximo três entre compras básicas, um envelope fechado, uma pequena caixa de remédio e uma reserva discreta.
- Em qualquer cena, remover objetos que não mudem sua leitura.

## Prioridade narrativa

A ordem de decisão visual é:

1. situação;
2. ação;
3. legibilidade;
4. personagem subordinado à cena;
5. cenário mínimo necessário.

FIN deve parecer integrado ao acontecimento. Câmera, fundo e props existem apenas para tornar a ação cotidiana clara, sem staging promocional ou acabamento de catálogo.

## Composição

- Preferir `wide shot` e `medium shot`; close é exceção narrativa.
- Quando presente, FIN ocupa em geral de 25% a 40% do quadro.
- Variar posição, distância de câmera, direção do olhar e ambiente.
- Evitar centralizar FIN por padrão.
- Preservar espaço para o contexto contar parte da história.
- Não inserir texto na arte quando `text_policy.mode` for `NONE`; em `OVERLAY`, manter os conteúdos declarados fora do prompt visual e deixar as superfícies limpas para aplicação determinística posterior.
- Construir primeiro uma imagem estática forte em 16:9; motion vem depois.

## Tipos de cena

- `CHARACTER_SCENE`: FIN vive a situação e conduz a leitura por ação e expressão.
- `OBJECT_SCENE`: um objeto ou pequeno conjunto de objetos conta o momento sem FIN, quando sua presença não acrescentaria informação.
- `ENVIRONMENT_SCENE`: o ambiente contextual é o assunto principal; FIN pode aparecer pequeno e integrado ao espaço.
- `SIMPLE_DATA_SCENE`: exceção rara para evidência que não possa ser mostrada com clareza por uma situação. Deve ser limpa, discreta e limitada a uma única relação visual.

## Identidade do FIN

O lock oficial é `FIN_V1`. As referências canônicas são `assets/character_bible/fin_turnaround.png` e `assets/character_bible/fin_poses.png`. Rosto, cabelo, proporções, roupa, gravata, paleta, contorno e estilo não podem ser redesenhados.

O lock completo vive somente em `config/character_fin.json`. Prompts podem usar o resumo canônico — versão `FIN_V1`, referências oficiais, proibição de redesign e traços essenciais — sem copiar toda a ficha. O resumo nunca substitui ou redefine a fonte completa.

## Linguagem proibida

Não usar como linguagem dominante:

- infográficos pesados, dashboards ou diagramas;
- textos gigantes, números incorporados e legendas dentro da imagem;
- setas, linhas, réguas, baselines, trilhos, engrenagens ou barras para substituir uma situação humana;
- colagens, telas divididas e acúmulo de símbolos;
- FIN centralizado e posando como apresentador em todas as cenas;
- fundos abstratos quando existe um ambiente cotidiano capaz de contar a ideia;
- reconstrução do pipeline SVG abstrato anterior.

`SIMPLE_DATA_SCENE` não reabre a direção antiga: é uma exceção editorial controlada, nunca o padrão.

## Motion

Motion é posterior e discreto: slow zoom in/out, pan leve, hold, crossfade, parallax sutil ou shake curto motivado pela ação. A animação apoia o quadro; não corrige uma composição fraca.

O render aplica somente presets enumerados no `scene_map.json`, sem inferir movimento a partir da imagem ou da prosa. A timeline é montada dinamicamente e precisa ser contínua. Assets pendentes, reprovados, ausentes, motion desconhecido ou duração incompatível bloqueiam a saída. Como o compositor de texto ainda não foi implementado, qualquer `text_policy.mode=OVERLAY` também bloqueia o render final em vez de omitir o texto silenciosamente.

Esta direção substitui oficialmente a abordagem anterior baseada em metáforas gráficas e composições abstratas.
