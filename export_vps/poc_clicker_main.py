"""
poc_clicker_main.py — MAESTRO da POC de cliques físicos (AutoHotkey) no TSE.

Estratégia por registro (igual ao pipeline original):
  1. Se tem nome_mae + data → tenta NÚMERO DO TÍTULO (4 campos, traz zona/seção/título).
  2. Se o título não retornar resultado (erro/divergência) → FALLBACK p/ SITUAÇÃO (só CPF).
  3. Se nem a situação aparecer → elegibilidade = 'regularizar_tse'.
  4. Persiste elegibilidade (+ título/zona/seção/etc quando houver) com --gravar.

Cada consulta tem 1 captcha manual (você resolve no Chrome + Ctrl+Shift+C).
O tse_clicker.ahk precisa estar RODANDO e calibrado (F9/F10).

USO:
  # título com fallback, CPF+mãe+data na mão, NÃO grava:
  python poc_clicker_main.py --cpf 15173248742 --mae "MARIA DA SILVA" --data 26/04/1966

  # só situação, CPF na mão:
  python poc_clicker_main.py --cpf 15173248742

  # pega 1 pendente do Supabase, fallback completo, NÃO grava (dry-run):
  python poc_clicker_main.py

  # pega 5 pendentes e GRAVA elegibilidade no Supabase:
  python poc_clicker_main.py --loop 5 --gravar
"""

import argparse
import os
import re
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

# Console do Windows costuma ser cp1252 e quebra com emoji/seta. Força UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bridge_helper as bridge
from tse_extracao_texto import parse_resultado_texto

# Logs no mesmo estilo do scraper original (cores + nome do tenant).
try:
    from utils import VERDE, VERMELHO, AMARELO, AZUL, NEGRITO, notificar
    from settings import nome_tenant
except Exception:  # fallback se rodar fora do projeto (ex.: --cpf solto)
    _id = lambda t: t
    VERDE = VERMELHO = AMARELO = AZUL = NEGRITO = _id
    def nome_tenant(tid):  # type: ignore
        return (tid or "?")[:8]
    def notificar(_msg):  # type: ignore
        pass


# ─────────────────────────────────────────────
# LOG EM ARQUIVO — Tee: tudo que vai pro console vai também pra logs/
# ─────────────────────────────────────────────
class _Tee:
    """Duplica stdout/stderr pra um arquivo de log, prefixando hora em cada linha."""

    def __init__(self, stream, arq):
        self._stream = stream
        self._arq = arq
        self._inicio_linha = True

    def write(self, s):
        self._stream.write(s)
        try:
            for ch in s:
                if self._inicio_linha and ch not in ("\r", "\n"):
                    self._arq.write(datetime.now().strftime("[%H:%M:%S] "))
                    self._inicio_linha = False
                self._arq.write(ch)
                if ch == "\n":
                    self._inicio_linha = True
            self._arq.flush()
        except Exception:
            pass  # log é conveniência — nunca pode quebrar o fluxo

    def flush(self):
        self._stream.flush()


def _ativar_log() -> Path:
    """Redireciona stdout/stderr pra Tee(console, logs/producao_AAAAMMDD.log)."""
    pasta = Path(__file__).resolve().parent / "logs"
    pasta.mkdir(exist_ok=True)
    caminho = pasta / f"producao_{datetime.now():%Y%m%d}.log"
    arq = open(caminho, "a", encoding="utf-8", buffering=1)
    sys.stdout = _Tee(sys.stdout, arq)
    sys.stderr = _Tee(sys.stderr, arq)
    return caminho


def _so_digitos(cpf: str) -> str:
    return re.sub(r"\D", "", cpf or "")


def _normalizar_data(raw) -> str:
    """DD/MM/AAAA. Tenta o normalizador do projeto; senão, regrinha local."""
    try:
        from utils import normalizar_data_br
        v = normalizar_data_br(raw)
        if v:
            return v
    except Exception:
        pass
    digs = re.sub(r"\D", "", str(raw or ""))
    if len(digs) == 8:
        if digs[:4].isdigit() and 1900 <= int(digs[:4]) <= 2100:  # YYYYMMDD
            return f"{digs[6:8]}/{digs[4:6]}/{digs[:4]}"
        return f"{digs[:2]}/{digs[2:4]}/{digs[4:8]}"             # DDMMYYYY
    return ""


def _data_do_registro(reg: dict) -> str:
    return _normalizar_data(reg.get("data_nascimento_br") or reg.get("data_nascimento"))


def _enriquecer(reg: dict, sb, gravar: bool, tenant: str, mae: str, data: str):
    """
    Enriquecimento ANTES do TSE: API B (principal) → DataSintese (fallback).
    Preenche nome_mae/data faltantes p/ habilitar a consulta de TÍTULO.
    Persiste no Supabase (se --gravar). Retorna (mae, data) atualizados.
    """
    from enriquecimento_lite import enriquecer_cpf
    cpf = _so_digitos(reg["cpf"])
    print(AZUL(f"  🔎 [{tenant}] enriquecendo (API B → DataSintese)..."))
    e = enriquecer_cpf(cpf, precisa_mae=not mae, precisa_data=not data)

    novo_mae = mae or (e.get("nome_mae") or "").strip()
    data_iso = e.get("data_nascimento")
    novo_data = data or _normalizar_data(data_iso)

    ganhou_mae = bool(novo_mae) and not mae
    ganhou_data = bool(novo_data) and not data
    if ganhou_mae or ganhou_data:
        origem = " + ".join(e.get("origem") or []) or "?"
        print(VERDE(f"     ✅ enriquecido [{origem}]: mãe={'sim' if novo_mae else 'não'} | data={novo_data or '—'}"))
        if gravar and sb is not None and reg.get("id"):
            payload = {}
            if ganhou_mae:
                payload["nome_mae"] = novo_mae
            if ganhou_data and data_iso:
                payload["data_nascimento"] = data_iso
                payload["data_nascimento_br"] = novo_data
            if payload:
                try:
                    sb.table(reg["_tabela"]).update(payload).eq("id", reg["id"]).execute()
                    print(VERDE(f"     💾 [{tenant}] enriquecimento gravado: {list(payload.keys())}"))
                except Exception as ex:
                    print(VERMELHO(f"     ⚠️  falha ao gravar enriquecimento: {ex}"))
        reg["nome_mae"] = novo_mae
    else:
        erros = "; ".join(e.get("erros") or []) or "sem dados"
        print(AMARELO(f"     ⚠️  enriquecimento não trouxe nada ({erros})"))
    return novo_mae, novo_data


def _consulta(modo: str, cpf: str, mae: str = "", data: str = "") -> dict:
    """Roda uma consulta via ponte AHK e devolve o dict parseado."""
    cpf = _so_digitos(cpf)
    cid = uuid.uuid4().hex[:8]  # identifica a consulta na ponte (anti-stale)
    # mãe por último: um '|' no nome não desloca os demais campos
    payload = "|".join([cid, modo, cpf, data or "", mae or ""])
    bridge.clear_output()
    bridge.write_input(payload)
    bridge.write_status("aguardando_entrada", cid)

    print(AZUL(f"  → [{modo}] AHK preenchendo e clicando Entrar..."))
    bridge.wait_for("esperando_captcha", timeout=120, token=cid)
    print(AMARELO(f"  🟡 [{modo}] Resolva o captcha (até 30s) — capturo sozinho ao renderizar. Senão, fica pendente."))
    notificar(f"🟡 TSE: captcha na tela ({modo} · CPF …{cpf[-4:]}) — 30s pra resolver.")
    # AHK avisa "timeout" em 30s se o captcha não for resolvido; 90s é só margem.
    bridge.wait_for("resultado_capturado", timeout=90, token=cid)

    res = parse_resultado_texto(bridge.read_output())
    res["cpf"] = cpf
    res["_modo"] = modo
    return res


def _mostrar(res: dict) -> None:
    print(AZUL(f"     📄 {res['texto_len']} chars"
               + (f" | cpf_pagina={res['cpf_pagina']}" if res.get("cpf_pagina") else "")))
    if res["erro_tecnico"]:
        print(VERMELHO(f"     ❌ erro técnico: {res['motivo_situacao']}"))
        return
    extras = [f"{k}={res[k]}" for k in ("titulo_eleitoral", "zona", "secao", "biometria") if res.get(k)]
    print(f"     situação={res['situacao']}" + (("  | " + " | ".join(extras)) if extras else ""))


# Mapeamento res(parser) → colunas REAIS do banco (porta de _coletar_campos_resultado).
def _coletar_campos(res: dict, dados: dict) -> None:
    from supabase_repo import mapear_elegibilidade
    if res.get("situacao"):
        dados["elegibilidade"] = mapear_elegibilidade(res["situacao"])
    if res.get("titulo_eleitoral"):
        dados["titulo_eleitoral"] = res["titulo_eleitoral"]
    if res.get("zona"):
        dados["zona_eleitoral"] = res["zona"]
    if res.get("secao"):
        dados["secao_eleitoral"] = res["secao"]
    if res.get("local_votacao"):
        dados["local_votacao"] = res["local_votacao"]
    if res.get("endereco_votacao"):
        dados["endereco_votacao"] = res["endereco_votacao"]
    if res.get("municipio"):
        dados["municipio_votacao"] = res["municipio"]
    if res.get("bairro"):
        dados["bairro_votacao"] = res["bairro"]
    if res.get("biometria"):
        dados["biometria"] = res["biometria"]
    if res.get("obrigacao_eleitoral"):
        dados["obrigacao_eleitoral"] = res["obrigacao_eleitoral"]
    if res.get("motivo_situacao"):
        dados["motivo_situacao"] = res["motivo_situacao"]
    if res.get("ano_situacao"):
        dados["ano_situacao"] = res["ano_situacao"]


def _classificar(res: dict, cpf_esperado: str) -> str:
    """
    Destino do resultado (porta de _classificar_resultado):
      'ok'             → válido, pode gravar
      'stale'          → página não corresponde ao CPF; mantém pendente (NÃO grava)
      'nao_cadastrado' → TSE não localizou → regularizar_tse
      'erro'           → página de erro técnico do TSE → regularizar_tse + fallback
    """
    situacao_raw = (res.get("situacao") or "").upper()
    if res.get("erro_tecnico"):
        return "erro"
    if "NAO_CADASTRADO" in situacao_raw or "NÃO CADASTRADO" in situacao_raw:
        return "nao_cadastrado"
    cpf_pagina = res.get("cpf_pagina")
    if not cpf_pagina or cpf_pagina != _so_digitos(cpf_esperado):
        return "stale"
    return "ok"


def _gravar_dados(sb, reg: dict, dados: dict, tenant: str = "") -> None:
    from supabase_repo import gravar_resultado
    if not dados:
        print(AMARELO("     (nada a gravar)"))
        return
    eleg = dados.get("elegibilidade", "?")
    try:
        gravar_resultado(sb, reg["_tabela"], reg["id"], dados)
        extras = {k: v for k, v in dados.items() if k != "elegibilidade"}
        print(VERDE(f"     💾 [{tenant}] {reg['_tabela']} gravado: elegibilidade={eleg}"
                    + (f" | {extras}" if extras else "")))
    except Exception as e:
        eleg = eleg if eleg != "?" else "regularizar_tse"
        print(VERMELHO(f"     ⚠️  [{tenant}] grava completa falhou ({e}); gravando só elegibilidade={eleg}"))
        gravar_resultado(sb, reg["_tabela"], reg["id"], {"elegibilidade": eleg})


def _processar(reg: dict, gravar: bool, sb=None, idx: int = 1, total: int = 1, enriquecer: bool = False):
    """Porta de processar_registro: (enriquecer)→título→fallback situação→regularizar_tse + anti-stale."""
    from supabase_repo import mapear_elegibilidade

    cpf = _so_digitos(reg["cpf"])
    tenant = nome_tenant(reg.get("tenant_id"))
    nome = (reg.get("nome") or "SEM NOME").strip()
    tabela = reg.get("_tabela") or "—"
    mae = (reg.get("nome_mae") or "").strip()
    data = _data_do_registro(reg)

    print(NEGRITO(f"\n[{idx}/{total}] [{tenant}] [{tabela}] 👤 {nome} | CPF: {cpf}"))

    # ── Enriquecimento ANTES (API B → DataSintese) p/ habilitar o TÍTULO ──
    if enriquecer and (not mae or not data):
        mae, data = _enriquecer(reg, sb, gravar, tenant, mae, data)

    tem_titulo = bool(mae and data)
    print(AZUL(f"  🧾 {'TÍTULO' if tem_titulo else 'SITUAÇÃO'} | mãe={'sim' if mae else 'não'} | data={data or '—'}"))
    dados: dict = {}

    # ── 1ª consulta: título (se tem dados) ou situação ──
    modo1 = "titulo" if tem_titulo else "situacao"
    r = _consulta(modo1, cpf, mae, data)
    _mostrar(r)
    desfecho = _classificar(r, cpf)

    if desfecho == "stale":
        print(AMARELO(f"  ⏭️  [{tenant}] STALE — página não bate com o CPF; mantém PENDENTE (não grava)."))
        return None

    if desfecho in ("nao_cadastrado", "erro"):
        dados["elegibilidade"] = "regularizar_tse"
    else:  # ok
        _coletar_campos(r, dados)
        dados.setdefault("elegibilidade", mapear_elegibilidade(r.get("situacao") or ""))

    # ── Fallback: se virou regularizar_tse e a 1ª foi título → tenta SITUAÇÃO (só CPF) ──
    if tem_titulo and dados.get("elegibilidade") == "regularizar_tse":
        print(AMARELO(f"  🔄 [{tenant}] Fallback SITUAÇÃO (só CPF)..."))
        rfb = _consulta("situacao", cpf)
        _mostrar(rfb)
        desf_fb = _classificar(rfb, cpf)
        sit_fb = (rfb.get("situacao") or "").upper()
        if desf_fb == "ok" and sit_fb in ("REGULAR", "CANCELADO", "SUSPENSO", "TRANSFERIDO"):
            print(VERDE(f"  ✅ [{tenant}] Situação (fallback): {sit_fb} — sobrescreve regularizar_tse"))
            _coletar_campos(rfb, dados)
        else:
            print(AMARELO(f"  → [{tenant}] Fallback não localizou — mantém regularizar_tse"))

    print(NEGRITO(f"  🎯 [{tenant}] elegibilidade final = {dados.get('elegibilidade')}"))

    if gravar and sb is not None and reg.get("id"):
        _gravar_dados(sb, reg, dados, tenant)
    else:
        print(AZUL(f"  (dry-run) gravaria: {dados}"))
    return dados


# ─────────────────────────────────────────────
# MODO PRODUÇÃO: enriquecimento (500, paralelo) + coleta de títulos (200), em loop
# ─────────────────────────────────────────────
def _status_enriq(e: dict) -> str:
    if e.get("nome_mae") or e.get("data_nascimento"):
        return "enriquecido"
    erros = [x.lower() for x in (e.get("erros") or [])]
    nao_encontrado = ("404", "sem_dados", "sem dados", "não encontrad", "nao encontrad",
                      "não localiz", "nao localiz")
    algum_tecnico = any(not any(k in er for k in nao_encontrado) for er in erros)
    return "erro" if algum_tecnico else "sem_dados"


def _enriquecer_um(sb, reg: dict):
    """Enriquece 1 registro (API B → DataSintese) e persiste. Retorna (status, origem).

    Usa cliente Supabase PRÓPRIO da thread (postgrest/httpx não garantem
    thread-safety com um cliente compartilhado) — o `sb` recebido é ignorado.
    """
    from enriquecimento_lite import enriquecer_cpf
    from supabase_repo import conectar_threadlocal
    sb = conectar_threadlocal()
    cpf = _so_digitos(reg["cpf"])
    falta_mae = not (reg.get("nome_mae") or "").strip()
    falta_data = not reg.get("data_nascimento")
    e = enriquecer_cpf(cpf, precisa_mae=falta_mae, precisa_data=falta_data)

    payload = {}
    if falta_mae and e.get("nome_mae"):
        payload["nome_mae"] = e["nome_mae"]
    if falta_data and e.get("data_nascimento"):
        payload["data_nascimento"] = e["data_nascimento"]
        br = _normalizar_data(e["data_nascimento"])
        if br:
            payload["data_nascimento_br"] = br
    if e.get("titulo_eleitor") and len(e["titulo_eleitor"]) >= 10:
        payload["titulo_eleitoral"] = e["titulo_eleitor"]

    status = _status_enriq(e)
    payload["enriquecimento_status"] = status
    try:
        sb.table(reg["_tabela"]).update(payload).eq("id", reg["id"]).execute()
    except Exception as ex:
        return "erro_update", str(ex)
    return status, ("+".join(e.get("origem") or []) or "-")


def _enriquecer_lote(sb, limite: int, workers: int = 8, por_tenant: "int | None" = None) -> int:
    """Fase A — enriquece em paralelo (sem captcha). Retorna nº enriquecidos.

    Se `por_tenant` for setado, pega até N registros de CADA tenant ativo
    (prioriza quem está sem nome_mae) em vez do lote global.
    """
    if por_tenant is not None:
        from supabase_repo import carregar_enriquecimento_pendente_por_tenant
        regs = carregar_enriquecimento_pendente_por_tenant(sb, por_tenant, priorizar_sem_mae=True)
    else:
        from supabase_repo import carregar_enriquecimento_pendente
        regs = carregar_enriquecimento_pendente(sb, limite)
    if not regs:
        print(AZUL("  🔎 (enriquecimento) nada pendente."))
        return 0
    total = len(regs)
    print(NEGRITO(AZUL(f"  🔎 Enriquecendo {total} (API B → Hashiro, {workers} workers)...")))
    stats = {"enriquecido": 0, "sem_dados": 0, "erro": 0, "erro_update": 0}
    concluidos = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_enriquecer_um, sb, r): r for r in regs}
        for f in as_completed(futs):
            reg = futs[f]
            tenant = nome_tenant(reg.get("tenant_id"))
            try:
                status, origem = f.result()
            except Exception as e:
                status, origem = "erro", f"exc:{type(e).__name__}"
            stats[status] = stats.get(status, 0) + 1
            concluidos += 1
            cor = VERDE if status == "enriquecido" else (AMARELO if status == "sem_dados" else VERMELHO)
            print(cor(f"    [{concluidos}/{total}] [{tenant}] {status} ({origem})"))
    print(VERDE(f"  ✅ enriquecimento: {stats}"))
    return stats.get("enriquecido", 0)


def _coletar_lote(sb, limite: int, por_tenant: "int | None" = None,
                  com_mae: bool = False) -> dict:
    """Fase B — coleta títulos (com captcha) p/ até `limite` registros.

    Modos:
      - com_mae=True: SÓ registros já enriquecidos COM nome_mae + data (fluxo TÍTULO puro).
      - por_tenant=N: pega até N registros de CADA tenant ativo (amostra).
      - default: lote global via `carregar_pendentes_n`.
    """
    if com_mae:
        from supabase_repo import carregar_com_mae_para_tse
        regs = carregar_com_mae_para_tse(sb, limite)
    elif por_tenant is not None:
        from supabase_repo import carregar_pendentes_por_tenant
        regs = carregar_pendentes_por_tenant(sb, por_tenant)
    else:
        from supabase_repo import carregar_pendentes_n
        regs = carregar_pendentes_n(sb, limite)
    if not regs:
        print(AMARELO("  🧾 (coleta) nada pendente."))
        return {}
    print(NEGRITO(AZUL(f"  🧾 Coletando títulos de {len(regs)} registro(s)...")))
    stats = {"apto": 0, "regularizar_tse": 0, "pendente": 0, "outros": 0}
    for i, reg in enumerate(regs, start=1):
        tenant = nome_tenant(reg.get("tenant_id"))
        try:
            dados = _processar(reg, gravar=True, sb=sb, idx=i, total=len(regs), enriquecer=False)
            if dados is None:
                stats["pendente"] += 1
            else:
                eleg = dados.get("elegibilidade")
                stats[eleg if eleg in stats else "outros"] += 1
        except TimeoutError as e:
            print(VERMELHO(f"  ⏭️  [{tenant}] {e} — mantém PENDENTE.")); stats["pendente"] += 1
        except RuntimeError as e:
            print(VERMELHO(f"  ❌ [{tenant}] {e} — pulando.")); stats["pendente"] += 1
        if i < len(regs):
            time.sleep(2.5)
    print(VERDE(f"  ✅ coleta: {stats}"))
    return stats


def _producao(sb, lote_enriq: int = 500, lote_coleta: int = 200, max_rodadas: int = 1000,
              por_tenant: "int | None" = None, com_mae: bool = False,
              pular_enriquecimento: bool = False) -> None:
    """
    Loop contínuo até zerar os 'nao_verificado':
      Fase A: enriquece até `lote_enriq` (rápido, paralelo).
      Fase B: coleta títulos de até `lote_coleta` (com captcha).
    Para se zerar OU se uma rodada não fizer progresso (pendentes travados).

    Se `por_tenant` for setado, cada fase pega N registros de CADA tenant ativo
    (modo amostra: útil pra testar N eleitores por tenant).
    """
    from supabase_repo import contar_pendentes_eleg, contar_com_mae_para_tse
    contador = contar_com_mae_para_tse if com_mae else contar_pendentes_eleg
    anterior = None
    for rodada in range(1, max_rodadas + 1):
        restantes = contador(sb)
        print(NEGRITO(AZUL(f"\n══════ RODADA {rodada} — {restantes} sem elegibilidade ══════")))
        if restantes == 0:
            print(NEGRITO(VERDE("🎉 ZERADO! Nada mais sem elegibilidade.")))
            notificar("🎉 TSE: fila ZERADA — nada mais sem elegibilidade.")
            return
        if anterior is not None and restantes >= anterior:
            print(NEGRITO(AMARELO(
                f"⚠️  Sem progresso desde a rodada anterior ({restantes}). "
                "Possíveis pendentes travados (captcha não resolvido / stale). Parando.")))
            notificar(f"⚠️ TSE: produção parou sem progresso ({restantes} pendentes travados).")
            return
        anterior = restantes
        if not pular_enriquecimento:
            _enriquecer_lote(sb, lote_enriq, por_tenant=por_tenant)   # Fase A
        _coletar_lote(sb, lote_coleta, por_tenant=por_tenant,
                      com_mae=com_mae)                                 # Fase B
    print(NEGRITO(AMARELO(f"⚠️  Atingiu o máximo de {max_rodadas} rodadas. Parando.")))


def main() -> int:
    ap = argparse.ArgumentParser(description="POC cliques físicos (AHK) no TSE c/ fallback")
    ap.add_argument("--cpf", help="CPF avulso (não lê Supabase)")
    ap.add_argument("--mae", default="", help="nome da mãe (p/ consulta de título no modo --cpf)")
    ap.add_argument("--data", default="", help="data nascimento DD/MM/AAAA (modo --cpf)")
    ap.add_argument("--loop", type=int, default=1, help="quantos pendentes processar (Supabase)")
    ap.add_argument("--gravar", action="store_true", help="grava no Supabase (padrão: dry-run)")
    ap.add_argument("--enriquecer", action="store_true",
                    help="enriquece nome_mae/data ANTES (API B → DataSintese) p/ habilitar o TÍTULO")
    ap.add_argument("--producao", action="store_true",
                    help="loop contínuo: enriquece (--lote-enriq) + coleta títulos (--lote-coleta) até zerar")
    ap.add_argument("--lote-enriq", type=int, default=500, help="tamanho do lote de enriquecimento (produção)")
    ap.add_argument("--lote-coleta", type=int, default=200, help="tamanho do lote de coleta de títulos (produção)")
    ap.add_argument("--max-rodadas", type=int, default=1000, help="máx. de rodadas no modo produção (controle/teste)")
    ap.add_argument("--por-tenant", type=int, default=None,
                    help="AMOSTRA: N registros por tenant ativo (sobrescreve --lote-enriq/--lote-coleta). "
                         "Ex.: --por-tenant 10 --producao --max-rodadas 1 --gravar → 10 eleitores × 7 tenants = 70 total.")
    ap.add_argument("--com-mae", action="store_true",
                    help="SÓ TSE-título: coleta apenas quem já está enriquecido COM nome_mae + data. "
                         "Combine com --pular-enriquecimento pra pular a fase A.")
    ap.add_argument("--pular-enriquecimento", action="store_true",
                    help="Pula a fase A (enriquecimento) no modo --producao. Útil c/ --com-mae.")
    args = ap.parse_args()

    os.system("color")  # habilita ANSI no terminal do Windows
    log_path = _ativar_log()
    # Limpa input/status deixados por uma execução anterior que morreu no meio —
    # sem isso o AHK processaria um pedido velho que ninguém está esperando.
    bridge.limpar_estado()
    print(NEGRITO(AZUL("═" * 60)))
    print(NEGRITO(AZUL("  POC TSE — número do título + fallback (cliques físicos AHK)")))
    print(NEGRITO(AZUL("═" * 60)))
    print(AZUL(f"  📝 log: {log_path}"))

    try:
        # ── Modo CPF avulso (sem banco, nunca grava) ──
        if args.cpf:
            reg = {
                "cpf": args.cpf, "nome_mae": args.mae,
                "data_nascimento_br": args.data, "_tabela": None, "id": None,
            }
            _processar(reg, gravar=False, enriquecer=args.enriquecer)
            return 0

        # ── Modo Supabase ──
        from supabase_repo import conectar, carregar_pendentes
        sb = conectar()

        # ── Modo PRODUÇÃO: enriquece 500 + coleta 200 em loop até zerar ──
        if args.producao:
            _producao(sb, lote_enriq=args.lote_enriq, lote_coleta=args.lote_coleta,
                      max_rodadas=args.max_rodadas, por_tenant=args.por_tenant,
                      com_mae=args.com_mae, pular_enriquecimento=args.pular_enriquecimento)
            return 0

        pendentes = carregar_pendentes(sb)
        if not pendentes:
            print(AMARELO("  Nada pendente no Supabase."))
            return 0

        alvo = pendentes[: args.loop]
        print(NEGRITO(f"  {len(alvo)} registro(s)"
                      + (" — MODO GRAVAÇÃO" if args.gravar else " — DRY-RUN (não grava)")))
        stats = {"apto": 0, "regularizar_tse": 0, "pendente": 0, "outros": 0}
        for i, reg in enumerate(alvo, start=1):
            tenant = nome_tenant(reg.get("tenant_id"))
            try:
                dados = _processar(reg, gravar=args.gravar, sb=sb, idx=i, total=len(alvo),
                                   enriquecer=args.enriquecer)
                if dados is None:               # stale → pendente
                    stats["pendente"] += 1
                else:
                    eleg = dados.get("elegibilidade")
                    stats[eleg if eleg in stats else "outros"] += 1
            except TimeoutError as e:
                print(VERMELHO(f"  ⏭️  [{tenant}] {e} — mantém PENDENTE, vai pro próximo."))
                stats["pendente"] += 1
            except RuntimeError as e:
                print(VERMELHO(f"  ❌ [{tenant}] {e} — pulando registro."))
                stats["pendente"] += 1
            if i < len(alvo):
                time.sleep(2.5)  # respiro entre consultas
        print(NEGRITO(VERDE(f"\n✔ Fim. apto={stats['apto']} | regularizar_tse={stats['regularizar_tse']} "
                            f"| pendente={stats['pendente']} | outros={stats['outros']}")))
        return 0
    except KeyboardInterrupt:
        print(AMARELO("\n⏹️  Interrompido (Ctrl+C). O que já foi gravado está salvo; "
                      "o resto continua pendente pro próximo run."))
        return 130
    except (TimeoutError, RuntimeError) as e:
        print(f"  ❌ {e}")
        return 1
    finally:
        bridge.write_status("pronto")


if __name__ == "__main__":
    raise SystemExit(main())
