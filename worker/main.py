"""
main.py — worker do portal: processa a fila batches/voter_records em 2 fases.

    Fase A (enriquecimento): pending → ready_tse  (APIs pagas, paralelo)
    Fase B (TSE):            ready_tse → done     (TSE autoatendimento, sequencial)

USO:
    python main.py --stub --limite 5        # dry-run: TSE falsificado, 5 registros
    python main.py --limite 20              # 1 rodada (5 padrão? não: lote A 500/B 200)
    python main.py --producao               # loop até zerar / sem progresso
    python main.py --producao --fase b      # só a fase B

Parada elegante: Ctrl+C ou criando o arquivo worker/STOP (checado entre
registros). Logs em worker/logs/worker_YYYYMMDD.log. Webhook (se
NOTIFY_WEBHOOK_URL estiver no .env) avisa de lotes concluídos e erros.

⚠️  Sem --stub, usa TseHttpClient: consultas de SITUAÇÃO (CPF-only) e de
    TÍTULO/ONDE-VOTAR (zona/seção/município, quando o registro tem mãe+data)
    implementadas contra o cad-api.tse.jus.br. Se o TSE rejeitar mãe+data
    (401), o worker faz fallback sozinho pra situação CPF-only.
"""

import argparse
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

# Console do Windows costuma ser cp1252 e quebra com acentos/emoji. Força UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

WORKER_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(WORKER_DIR))

from config_worker import garantir_export_vps_no_path, load_config  # noqa: E402

garantir_export_vps_no_path()  # path p/ tse_extracao_texto / utils

from tse_extracao_texto import parse_resultado_texto  # noqa: E402,F401  (fallback texto; o HTTP real devolve dict já parseado)
from utils import notificar, normalizar_data_br  # noqa: E402

import fase_a  # noqa: E402
import repo  # noqa: E402
import tse_client  # noqa: E402

LOTE_A = 500          # registros por rodada na fase A
LOTE_B = 200          # registros por rodada na fase B
PAUSA_TSE = 2.5       # pacing entre consultas TSE (jitter até +1.5s → 2.5–4s)
MAX_TENTATIVAS = 5    # stale/erro técnico repetido → status='error' (review manual)
ARQUIVO_STOP = WORKER_DIR / "STOP"


# ─────────────────────────────────────────────
# LOG EM ARQUIVO — Tee: console + logs/worker_YYYYMMDD.log
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
    pasta = WORKER_DIR / "logs"
    pasta.mkdir(exist_ok=True)
    caminho = pasta / f"worker_{datetime.now():%Y%m%d}.log"
    arq = open(caminho, "a", encoding="utf-8", buffering=1)
    sys.stdout = _Tee(sys.stdout, arq)
    sys.stderr = _Tee(sys.stderr, arq)
    return caminho


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def _so_digitos(cpf: str) -> str:
    return re.sub(r"\D", "", cpf or "")


def _parar_pedido() -> bool:
    """True se o arquivo STOP existe (o arquivo é consumido ao parar)."""
    if ARQUIVO_STOP.exists():
        try:
            ARQUIVO_STOP.unlink()
        except Exception:
            pass
        return True
    return False


def _classificar(res: dict, cpf_esperado: str) -> str:
    """
    Destino do resultado (porta de poc_clicker_main._classificar):
      'ok'             → válido, pode gravar
      'stale'          → página não corresponde ao CPF; NÃO grava, re-tenta
      'nao_cadastrado' → TSE não localizou → regularizar_tse (gravado como done)
      'erro'           → página de erro técnico do TSE → re-tentar; esgota → error
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


# ─────────────────────────────────────────────
# FASE B — consulta TSE (sequencial, com pacing e anti-stale)
# ─────────────────────────────────────────────
def _rodar_fase_b(limite: int, sb, processados_antes: int = 0,
                  limite_total: "int | None" = None) -> dict:
    regs = repo.pegar_prontos_tse(limite, sb=sb)
    stats = {"processados": 0, "apto": 0, "inapto": 0, "regularizar_tse": 0,
             "stale": 0, "erro": 0, "interrompido": False}
    if not regs:
        print("  🧾 (fase B) nada com status='ready_tse'.")
        return stats

    cliente_tse = tse_client.cliente()
    paca = cliente_tse.pacing_interno  # HTTP real já faz pacing interno
    aviso_titulo = False  # loga 1x o fallback de título→situação por lote

    print(f"  🧾 Fase B — consultando TSE de {len(regs)} registro(s)...")
    for i, reg in enumerate(regs, start=1):
        if limite_total is not None and processados_antes + stats["processados"] >= limite_total:
            stats["interrompido"] = True
            break
        if _parar_pedido():
            print("  🛑 Arquivo STOP detectado — interrompendo entre registros.")
            stats["interrompido"] = True
            break

        cpf = _so_digitos(reg["cpf"])
        mae = (reg.get("nome_mae") or "").strip() or None
        data_iso = (reg.get("data_nascimento") or "").strip() or None
        data_br = normalizar_data_br(data_iso) if data_iso else None
        tentativas = reg.get("attempts") or 0

        # Escolha da consulta: título rica (se implementada) → fallback situação
        usar_titulo = bool(mae and data_br and getattr(cliente_tse, "tem_titulo", False))
        modo = "TÍTULO" if usar_titulo else "SITUAÇÃO"
        if mae and data_br and not usar_titulo and not aviso_titulo:
            print("      ℹ️  endpoint de título/zona ainda pendente do probe — "
                  "registros com mãe+data usam a consulta de SITUAÇÃO "
                  "(sem zona/seção/município) por enquanto.")
            aviso_titulo = True

        print(f"    [{i}/{len(regs)}] CPF …{cpf[-4:]} ({modo})")
        try:
            repo.marcar(reg["id"], "checking", sb=sb)
            if usar_titulo:
                try:
                    res = cliente_tse.consultar_titulo(cpf, data_br, mae)
                except NotImplementedError:
                    usar_titulo = False
                    modo = "SITUAÇÃO"
                    res = tse_client.consultar_tse(cpf, nome_mae=mae,
                                                   data_nascimento=data_br)
            else:
                res = tse_client.consultar_tse(cpf, nome_mae=mae,
                                               data_nascimento=data_br)
            raw_text = res.pop("_raw", "") or ""
        except Exception as e:
            print(f"      ❌ erro na consulta: {type(e).__name__}: {e}")
            repo.marcar(reg["id"], "error", sb=sb)
            stats["erro"] += 1
            notificar(f"❌ TSE worker: erro consultando CPF …{cpf[-4:]}: {e}")
            continue

        if desfecho == "stale":
            stats["stale"] += 1
            # anti-stale: NÃO grava. Volta p/ ready_tse; esgotando tentativas → error
            if tentativas + 1 >= MAX_TENTATIVAS:
                repo.marcar(reg["id"], "error", sb=sb)
                print(f"      ⏭️  STALE (cpf_pagina={res.get('cpf_pagina')}) — "
                      f"tentativas esgotadas → error.")
            else:
                repo.marcar_lote([reg["id"]], "ready_tse", sb=sb)
                print(f"      ⏭️  STALE (cpf_pagina={res.get('cpf_pagina')}) — "
                      f"mantém ready_tse, tenta na próxima rodada.")
        elif desfecho == "erro":
            stats["erro"] += 1
            if tentativas + 1 >= MAX_TENTATIVAS:
                repo.marcar(reg["id"], "error", sb=sb)
                print(f"      ❌ erro técnico persistente → error "
                      f"({res.get('motivo_situacao')})")
            else:
                repo.marcar_lote([reg["id"]], "ready_tse", sb=sb)
                print(f"      ⚠️  erro técnico ({res.get('motivo_situacao')}) — "
                      f"re-tenta na próxima rodada.")
        else:  # ok / nao_cadastrado → persiste e fecha o registro
            try:
                repo.gravar_resultado_tse(reg["id"], res, raw_text, sb=sb)
                eleg = repo.mapear_elegibilidade(res.get("situacao") or "")
                if eleg == "apto":
                    stats["apto"] += 1
                elif eleg == "regularizar_tse":
                    stats["regularizar_tse"] += 1
                else:
                    stats["inapto"] += 1
                extras = [f"{k}={res[k]}" for k in ("titulo_eleitoral", "zona", "secao")
                          if res.get(k)]
                print(f"      ✅ elegibilidade={eleg}"
                      + (f" | {' | '.join(extras)}" if extras else ""))
            except Exception as e:
                stats["erro"] += 1
                repo.marcar(reg["id"], "error", sb=sb)
                print(f"      ❌ falha ao gravar resultado: {e}")
                notificar(f"❌ TSE worker: falha ao gravar CPF …{cpf[-4:]}: {e}")

        if i < len(regs) and not paca:
            time.sleep(PAUSA_TSE + random.uniform(0, 1.5))  # 2.5–4s anti-ban

    print(f"  ✅ fase B: {stats['processados']} processados | apto={stats['apto']} | "
          f"inapto={stats['inapto']} | regularizar_tse={stats['regularizar_tse']} | "
          f"stale={stats['stale']} | erro={stats['erro']}")
    return stats


# ─────────────────────────────────────────────
# LOOP DE PRODUÇÃO
# ─────────────────────────────────────────────
def _rodada(fase: str, lote_a: int, lote_b: int, sb,
            processados_b: int, limite_total: "int | None") -> dict:
    feito = {"a": 0, "b": 0}
    if fase in ("a", "ambas"):
        stats_a = fase_a.rodar_fase_a(lote_a, sb=sb)
        feito["a"] = stats_a["processados"]
    if fase in ("b", "ambas"):
        stats_b = _rodar_fase_b(lote_b, sb,
                                processados_antes=processados_b,
                                limite_total=limite_total)
        feito["b"] = stats_b["processados"]
        feito["interrompido"] = stats_b.get("interrompido", False)
    return feito


def main() -> int:
    ap = argparse.ArgumentParser(description="Worker do portal — fila TSE (2 fases)")
    ap.add_argument("--producao", action="store_true",
                    help="loop contínuo até zerar a fila / parar sem progresso "
                         "(padrão: 1 rodada)")
    ap.add_argument("--limite", type=int, default=None,
                    help="máx. de registros processados por fase nesta execução")
    ap.add_argument("--fase", choices=["a", "b", "ambas"], default="ambas",
                    help="qual fase rodar (padrão: ambas)")
    ap.add_argument("--stub", action="store_true",
                    help="usa TseStubClient (dry-run sem rede — consulta de "
                         "situação real já funciona sem este flag)")
    args = ap.parse_args()

    os.system("color")  # habilita ANSI no terminal do Windows
    log_path = _ativar_log()
    print("═" * 60)
    print("  WORKER PORTAL — enriquecimento + consulta TSE (fila batches)")
    print("═" * 60)
    print(f"  📝 log: {log_path}")

    try:
        load_config()  # falha cedo (mensagem pt-BR) se .env.portal faltar
    except RuntimeError as e:
        print(f"  {e}")
        return 1

    tse_client.usar_cliente(tse_client.TseStubClient() if args.stub
                            else tse_client.TseHttpClient())
    modo = "STUB (dry-run)" if args.stub else "HTTP REAL (situação + onde-votar)"
    print(f"  🔌 cliente TSE: {modo}")

    sb = repo.conectar()
    lote_a = min(LOTE_A, args.limite) if args.limite else LOTE_A
    lote_b = min(LOTE_B, args.limite) if args.limite else LOTE_B

    processados_b_total = 0
    try:
        if not args.producao:
            _rodada(args.fase, lote_a, lote_b, sb, 0, args.limite)
        else:
            anterior = None
            rodada = 0
            while True:
                rodada += 1
                restantes = repo.contar_pendentes(sb)
                print(f"\n══════ RODADA {rodada} — {restantes} registro(s) aberto(s) ══════")
                if restantes == 0:
                    print("🎉 Fila ZERADA.")
                    notificar("🎉 TSE worker: fila zerada — nada mais a processar.")
                    break
                if _parar_pedido():
                    print("🛑 Arquivo STOP detectado — encerrando.")
                    break
                if anterior is not None and restantes >= anterior:
                    print(f"⚠️  Sem progresso ({restantes} abertos, igual à rodada "
                          f"anterior). Possíveis registros travados — parando.")
                    notificar(f"⚠️ TSE worker: parado sem progresso "
                              f"({restantes} registros abertos).")
                    break
                anterior = restantes
                feito = _rodada(args.fase, lote_a, lote_b, sb,
                                processados_b_total, args.limite)
                processados_b_total += feito["b"]
                finalizados = repo.atualizar_batches(sb)
                for bid in finalizados:
                    notificar(f"🏁 TSE worker: lote {bid[:8]} concluído (todos os "
                              f"registros processados).")
                if feito.get("interrompido"):
                    break
                if feito["a"] == 0 and feito["b"] == 0:
                    print("⚠️  Rodada sem trabalho — encerrando.")
                    break

        finalizados = repo.atualizar_batches(sb)
        print(f"\n✔ Fim. Lotes finalizados nesta execução: {len(finalizados)}.")
        return 0
    except KeyboardInterrupt:
        print("\n⏹️  Interrompido (Ctrl+C). O que já foi gravado está salvo; o resto "
              "continua na fila para o próximo run.")
        notificar("⏹️ TSE worker: interrompido (Ctrl+C).")
        return 130
    except Exception as e:
        print(f"\n❌ Erro fatal: {type(e).__name__}: {e}")
        notificar(f"❌ TSE worker: erro fatal: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
