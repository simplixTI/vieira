"""
fase_a.py — enriquecimento em lote (API B → Hashiro → DataSintese).

Roda enriquecer_cpf() do export_vps em ThreadPoolExecutor (8 workers) sobre
os voter_records com status='pending', e persiste o resultado via repo.

Conta as CHAMADAS às APIs pagas (por fonte: API-B/Hashiro/DataSintese) a
partir das tags 'origem' (sucessos) e 'erros' (falhas) — assim o operador
acompanha o gasto: o log da linha "enriquecimento: N chamadas".
"""

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from config_worker import garantir_export_vps_no_path

garantir_export_vps_no_path()  # precisa estar no path antes do import abaixo

from enriquecimento_lite import enriquecer_cpf  # noqa: E402

import repo  # noqa: E402

WORKERS_PADRAO = 8

_contador_lock = threading.Lock()
_chamadas = 0


def _contar_chamadas(e: dict) -> int:
    """Quantas APIs foram acionadas pra este CPF (sucessos + falhas, por fonte)."""
    fontes = set()
    for tag in (e.get("origem") or []):
        fontes.add(str(tag).split("(")[0].strip())
    for erro in (e.get("erros") or []):
        fontes.add(str(erro).split(":")[0].strip())
    return len(fontes)


def _enriquecer_um(reg: dict) -> str:
    """Marca 'enriching', enriquece e persiste. Retorna o desfecho."""
    global _chamadas
    sb = repo.cliente_threadlocal()
    try:
        repo.marcar(reg["id"], "enriching", sb=sb)
    except Exception:
        pass  # marcação é conveniência; não aborta o enriquecimento

    falta_mae = not (reg.get("nome_mae") or "").strip()
    falta_data = not (reg.get("data_nascimento") or "").strip()
    try:
        e = enriquecer_cpf(reg["cpf"], precisa_mae=falta_mae, precisa_data=falta_data)
    except Exception:
        try:
            repo.marcar(reg["id"], "error", sb=sb)
        except Exception:
            pass
        return "erro"

    with _contador_lock:
        _chamadas += _contar_chamadas(e)

    if e.get("nome_mae") or e.get("data_nascimento") or e.get("titulo_eleitor"):
        try:
            repo.gravar_enriquecimento(reg["id"], e, sb=sb)
        except Exception:
            try:
                repo.marcar(reg["id"], "error", sb=sb)
            except Exception:
                pass
            return "erro_gravacao"
        return "enriquecido"

    # nada trouxe mãe/data/título: ainda assim avança p/ TSE em modo CPF-only
    try:
        repo.gravar_enriquecimento(reg["id"], {}, sb=sb)
    except Exception:
        return "erro_gravacao"
    return "sem_dados"


def rodar_fase_a(limite: int = 500, workers: int = WORKERS_PADRAO,
                 sb=None) -> dict:
    """
    Enriquece até `limite` registros pendentes em paralelo.

    Retorna stats: {processados, enriquecido, sem_dados, erro, erro_gravacao,
    chamadas}.
    """
    global _chamadas
    with _contador_lock:
        _chamadas = 0

    regs = repo.pegar_pendentes_enriquecimento(limite, sb=sb)
    if not regs:
        print("  🔎 (fase A) nada pendente.")
        return {"processados": 0, "enriquecido": 0, "sem_dados": 0,
                "erro": 0, "erro_gravacao": 0, "chamadas": 0}

    total = len(regs)
    print(f"  🔎 Fase A — enriquecendo {total} registro(s) "
          f"(API B → Hashiro → DataSintese, {workers} workers)...")
    stats = {"processados": total, "enriquecido": 0, "sem_dados": 0,
             "erro": 0, "erro_gravacao": 0, "chamadas": 0}
    concluidos = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_enriquecer_um, r): r for r in regs}
        for f in as_completed(futs):
            try:
                desfecho = f.result()
            except Exception:
                desfecho = "erro"
            stats[desfecho] = stats.get(desfecho, 0) + 1
            concluidos += 1
            print(f"    [{concluidos}/{total}] {desfecho}")

    with _contador_lock:
        stats["chamadas"] = _chamadas
    print(f"  ✅ fase A: enriquecido={stats['enriquecido']} | "
          f"sem_dados={stats['sem_dados']} | erro={stats['erro']} | "
          f"erro_gravacao={stats['erro_gravacao']}")
    print(f"  💰 enriquecimento: {stats['chamadas']} chamadas às APIs pagas")
    return stats
