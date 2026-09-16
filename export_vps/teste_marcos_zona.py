"""
teste_marcos_zona.py — TESTE AO VIVO (dry-run, NÃO grava) do tenant Marcos Tavares.

Pega N registros "travados" (elegibilidade apto/inapto, COM nome_mae + data, mas
SEM zona) e roda a consulta de NÚMERO DO TÍTULO ao vivo pela ponte AHK, pra
descobrir se a divergência é PERMANENTE (dado ruim) ou TRANSITÓRIA (sessão ruim).

NÃO altera o banco. Precisa do tse_clicker.ahk RODANDO e calibrado (Ctrl+Shift+K),
Chrome aberto na página de número do título, e um humano pros captchas.

USO (com o venv que funciona):
    "d:\\CLAUDE\\AUTOMACAO TSE\\.venv\\Scripts\\python.exe" teste_marcos_zona.py
    # ou definindo a quantidade:
    "...python.exe" teste_marcos_zona.py --n 20
"""
import argparse
import sys

sys.path.insert(0, ".")

from config import load_config
from supabase import create_client, ClientOptions
import poc_clicker_main as maestro

MARCOS = "0b479271-aac5-46ba-93f0-24ea55ebf66c"
_has = lambda v: v not in (None, "", " ")


def _carregar_travados(sb, limite: int) -> list:
    """Registros do Marcos: apto/inapto + mae + data + SEM zona (os 'travados')."""
    alvo = []
    for tabela in ("eleitores", "liderancas"):
        off = 0
        while len(alvo) < limite:
            r = (sb.table(tabela)
                   .select("id, tenant_id, cpf, nome_mae, data_nascimento, "
                           "data_nascimento_br, nome, elegibilidade, zona_eleitoral")
                   .eq("tenant_id", MARCOS)
                   .in_("elegibilidade", ["apto", "inapto_cancelado", "inapto_suspenso",
                                          "inapto_transferido"])
                   .not_.is_("nome_mae", "null")
                   .not_.is_("data_nascimento", "null")
                   .is_("zona_eleitoral", "null")
                   .range(off, off + 199).execute())
            rows = r.data or []
            if not rows:
                break
            for x in rows:
                if _has(x.get("cpf")) and _has(x.get("nome_mae")) and _has(x.get("data_nascimento")):
                    x["_tabela"] = tabela
                    alvo.append(x)
                    if len(alvo) >= limite:
                        break
            if len(rows) < 200:
                break
            off += 200
    return alvo[:limite]


def main() -> int:
    ap = argparse.ArgumentParser(description="Teste ao vivo (dry-run) de zona no Marcos")
    ap.add_argument("--n", type=int, default=20, help="quantos registros testar (default 20)")
    args = ap.parse_args()

    cfg = load_config()
    sb = create_client(cfg.SUPABASE_URL, cfg.SUPABASE_SERVICE_KEY,
                       options=ClientOptions(postgrest_client_timeout=120))

    regs = _carregar_travados(sb, args.n)
    if not regs:
        print("Nada travado encontrado. (Marcos pode já estar resolvido.)")
        return 0

    print("=" * 64)
    print(f"  TESTE AO VIVO — {len(regs)} travados do Marcos (DRY-RUN, não grava)")
    print("  Objetivo: ver se a consulta de TÍTULO traz zona/seção agora.")
    print("=" * 64)

    com_zona = 0
    divergiu = 0
    pendentes = 0
    for i, reg in enumerate(regs, start=1):
        try:
            dados = maestro._processar(reg, gravar=False, sb=sb, idx=i, total=len(regs),
                                       enriquecer=False)
        except (TimeoutError, RuntimeError) as e:
            print(f"  ⏭️  pulou ({e})")
            pendentes += 1
            continue
        if dados is None:               # stale
            pendentes += 1
            continue
        if _has(dados.get("zona_eleitoral")):
            com_zona += 1
        elif dados.get("elegibilidade") in ("apto", "inapto_cancelado", "inapto_suspenso",
                                            "inapto_transferido"):
            # resolveu situação mas título não trouxe zona → divergência persistente
            divergiu += 1
        else:
            divergiu += 1
        import time
        if i < len(regs):
            time.sleep(2.5)

    testados = len(regs)
    print("\n" + "=" * 64)
    print(f"  RESULTADO DO TESTE ({testados} testados):")
    print(f"    ✅ trouxeram ZONA agora ...... {com_zona}")
    print(f"    ❌ divergiu de novo (sem zona)  {divergiu}")
    print(f"    ⏭️  pendente/stale ............ {pendentes}")
    print("=" * 64)
    if com_zona >= max(1, testados * 0.5):
        print("  ➜ Maioria trouxe zona: a divergência era TRANSITÓRIA (sessão ruim).")
        print("    Vale resetar os ~8.4k travados do Marcos p/ reprocessar.")
    elif com_zona == 0:
        print("  ➜ Ninguém trouxe zona: divergência PERMANENTE (dado de mãe/data")
        print("    não bate no TSE). Resetar NÃO adianta — melhorar o dado antes.")
    else:
        print("  ➜ Misto: parte é dado ruim, parte recuperável. Reset parcial faz sentido.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
