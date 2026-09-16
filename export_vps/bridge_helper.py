"""
bridge_helper.py — ponte por arquivos entre o Python (maestro) e o AutoHotkey (worker).

Protocolo simples: a pasta bridge/ tem 3 arquivos.
  - input.txt   : Python escreve o pedido de consulta pro AHK
  - output.txt  : AHK despeja o texto da página (Ctrl+A/Ctrl+C) pro Python ler
  - status.txt  : estado compartilhado, formato "estado|detalhe"

Formato do input.txt (5 campos — o UUID vem PRIMEIRO e a mãe por ÚLTIMO,
assim um '|' acidental no nome da mãe não desloca os demais campos):
  uuid|modo|cpf|data_br|nome_mae

Estados (status.txt) — todos carregam o UUID da consulta quando houver:
  aguardando_entrada|uuid   -> Python pôs uma consulta em input.txt; AHK deve processar
  processando|modo|cpf|uuid -> AHK começou (navegou/clicou)
  esperando_captcha|modo|cpf|uuid -> AHK preencheu e clicou Entrar; espera o resultado
  resultado_capturado|modo|cpf|uuid -> AHK copiou a página pra output.txt
  timeout|modo|cpf|uuid     -> captcha não resolvido a tempo
  erro|<msg>                -> algo falhou no AHK
  pronto                    -> Python terminou o ciclo (idle)

O UUID identifica a consulta: se o Python morrer no meio e deixar um
status/input velho pra trás, a próxima execução ignora qualquer status
cujo UUID não seja o da consulta corrente (anti-stale de protocolo).

Todas as escritas são ATÔMICAS (arquivo temporário + os.replace) pra que
o AHK nunca leia um arquivo pela metade.
"""

import os
import time
from pathlib import Path

BRIDGE = Path(__file__).resolve().parent / "bridge"
BRIDGE.mkdir(exist_ok=True)

INPUT_FILE = BRIDGE / "input.txt"
OUTPUT_FILE = BRIDGE / "output.txt"
STATUS_FILE = BRIDGE / "status.txt"


def _escrever_atomico(path: Path, txt: str) -> None:
    """Grava em arquivo temporário e renomeia — leitores nunca veem conteúdo parcial."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(txt, encoding="utf-8")
    # os.replace é atômico no mesmo volume; retry se o leitor segurar o arquivo
    for _ in range(12):
        try:
            os.replace(tmp, path)
            return
        except OSError:
            time.sleep(0.025)
    # última tentativa sem retry — deixa a exceção subir se realmente falhar
    os.replace(tmp, path)


def write_status(estado: str, detalhe: str = "") -> None:
    txt = estado if not detalhe else f"{estado}|{detalhe}"
    _escrever_atomico(STATUS_FILE, txt)


def read_status() -> str:
    try:
        # utf-8-sig descarta BOM se o AHK gravar com ele
        return STATUS_FILE.read_text(encoding="utf-8-sig").strip()
    except FileNotFoundError:
        return ""


def estado_atual() -> str:
    """Só o primeiro campo do status (antes do '|')."""
    return read_status().split("|", 1)[0]


def write_input(payload: str) -> None:
    """Escreve a linha de entrada pro AHK. Formato: 'uuid|modo|cpf|data_br|nome_mae'."""
    _escrever_atomico(INPUT_FILE, payload)


def read_output() -> str:
    try:
        return OUTPUT_FILE.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return ""


def clear_output() -> None:
    try:
        OUTPUT_FILE.unlink()
    except FileNotFoundError:
        pass


def limpar_estado() -> None:
    """Remove input/output velhos e volta o status pra 'pronto' (início de sessão)."""
    clear_output()
    try:
        INPUT_FILE.unlink()
    except FileNotFoundError:
        pass
    write_status("pronto")


def wait_for(estado_alvo: str, timeout: float = 300.0, poll: float = 0.5,
             token: str = "") -> str:
    """
    Espera o status virar `estado_alvo`. Levanta RuntimeError se o AHK reportar
    'erro', ou TimeoutError se estourar o tempo.

    Se `token` (UUID da consulta) for informado, só aceita status que o
    contenham — status de uma consulta anterior (stale) é ignorado.
    """
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = read_status()
        head = st.split("|", 1)[0]
        if token and token not in st:
            # status de outra consulta (ou 'pronto'/vazio) — não é nosso
            time.sleep(poll)
            continue
        if head == estado_alvo or estado_alvo in st:
            return st
        if head == "timeout":
            # captcha não resolvido a tempo → registro fica PENDENTE, vai pro próximo
            raise TimeoutError(f"captcha não resolvido a tempo ({st})")
        if head == "erro":
            raise RuntimeError(f"AHK reportou erro: {st}")
        time.sleep(poll)
    raise TimeoutError(f"Timeout esperando '{estado_alvo}' (status atual: '{read_status()}')")
