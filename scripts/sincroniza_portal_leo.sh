#!/usr/bin/env bash
# Sincroniza o portal do Leo Vieira Filho (simplixTI/leovieirafilho) com portal/.
#
# POR QUE ISTO EXISTE: sao dois portais publicados do mesmo codigo. app.js e
# styles.css sao IDENTICOS nos dois; so config.js (FEATURES.celular) e
# index.html (nome na marca) diferem. Sem este script e facil publicar uma
# mudanca so pra Priscila e deixar o Leo sem ela — foi assim que o dedupe de
# CPF quase saiu pela metade.
#
# O que faz: copia os arquivos comuns, aplica no index.html do Leo as mudancas
# estruturais do index.html daqui PRESERVANDO a marca dele, e abre pra revisao
# antes de qualquer push. NAO faz push sozinho.
#
# Uso:  bash scripts/sincroniza_portal_leo.sh [/caminho/do/clone]

set -euo pipefail

AQUI="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ORIGEM="$AQUI/portal"
CLONE="${1:-$AQUI/../leovieirafilho}"

if [ ! -d "$CLONE/.git" ]; then
  echo "Clone nao encontrado em: $CLONE"
  echo "Rode:  git clone https://github.com/simplixTI/leovieirafilho \"$CLONE\""
  exit 1
fi

git -C "$CLONE" pull --ff-only

# Arquivos 100% comuns: copia direto.
for f in app.js styles.css; do
  cp "$ORIGEM/$f" "$CLONE/$f"
  echo "copiado: $f"
done

# config.js e index.html NAO sao copiados — sao especificos do tenant.
# Mudancas estruturais no index.html (novos ids/blocos) precisam ser aplicadas
# a mao no do Leo. O diff abaixo mostra exatamente o que falta.
echo
echo "=== index.html: diferencas (esperado APENAS o nome na marca) ==="
# --strip-trailing-cr: os clones podem ter finais de linha diferentes (CRLF/LF)
# e sem isto o diff acusa o arquivo inteiro, escondendo a mudanca real.
diff --strip-trailing-cr "$CLONE/index.html" "$ORIGEM/index.html" || true
echo
echo "=== config.js: diferencas (esperado APENAS FEATURES.celular) ==="
diff --strip-trailing-cr "$CLONE/config.js" "$ORIGEM/config.js" || true
echo
echo "Se o diff do index.html mostrar blocos novos alem do nome da marca,"
echo "aplique-os a mao em $CLONE/index.html antes de publicar."

# Cache-busting: sem bump do ?v=, o navegador do cliente continua rodando o
# app.js velho contra o banco novo. Foi o que quebrou o portal do Leo em 19/09.
V_ORIGEM=$(grep -o 'app\.js?v=[0-9a-z]*' "$ORIGEM/index.html" | head -1 | cut -d= -f2)
V_CLONE=$(grep -o 'app\.js?v=[0-9a-z]*' "$CLONE/index.html" | head -1 | cut -d= -f2)
echo
if [ -z "$V_ORIGEM" ]; then
  echo "AVISO: portal/index.html nao tem ?v= em app.js — cache-busting ausente."
elif [ "$V_ORIGEM" != "$V_CLONE" ]; then
  echo "ATENCAO: versao de cache difere — origem='$V_ORIGEM' clone='$V_CLONE'."
  echo "Atualize o ?v= no index.html do Leo para '$V_ORIGEM' antes de publicar,"
  echo "senao o cliente dele continua com o app.js antigo em cache."
else
  echo "cache-busting: ?v=$V_ORIGEM igual nos dois. OK."
fi
echo
git -C "$CLONE" status --short
echo
echo "Para publicar:  git -C \"$CLONE\" add -A && git -C \"$CLONE\" commit && git -C \"$CLONE\" push"
