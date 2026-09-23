// Configuração do portal. Preencha SUPABASE_ANON_KEY com a chave anon do projeto,
// ou use localStorage 'portal_supabase_url' / 'portal_anon_key' para testes sem editar o arquivo.
(function () {
  var cfg = {
    SUPABASE_URL: 'https://wipthjinvcyglbeuxxsb.supabase.co',
    SUPABASE_ANON_KEY: 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6IndpcHRoamludmN5Z2xiZXV4eHNiIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODk1MjUxMzAsImV4cCI6MjEwNTEwMTEzMH0.s-IShdUxLV0o4tIpLutnpo9sMDivmQu34n1oqwmm0pA',
    // Features por tenant. Override no portal específico do cliente.
    FEATURES: {
      celular: false,   // Leo Vieira Filho seta true no config.js dele
      titulo: true,     // avulsa aceita título de eleitor (12 dígitos, só aptidão)
    },

    // ── MODO MANUTENÇÃO ──────────────────────────────────────────────────
    // Suspende ENVIOS NOVOS (lote e consulta avulsa) sem derrubar o portal:
    // o cliente continua entrando, vendo os resultados que já tem e exportando
    // o CSV. Ligado em 23/09/2026 porque o TSE passou a exigir captcha no
    // endpoint de token e nenhuma consulta nova conclui (STATUS.md §11).
    //
    // PRA RELIGAR: `ativo: false` aqui, bump do ?v= no index.html, deploy.
    // Não precisa mexer em app.js. Lembre dos DOIS portais.
    MANUTENCAO: {
      ativo: false,
      mensagem: 'O TSE ativou uma proteção que bloqueia consultas automáticas, '
        + 'e as verificações estão pausadas até o serviço ser restabelecido. '
        + 'Os resultados já concluídos continuam disponíveis aqui e podem ser '
        + 'exportados normalmente. Avisaremos assim que voltar.',
    },
  };

  try {
    var url = localStorage.getItem('portal_supabase_url');
    var key = localStorage.getItem('portal_anon_key');
    if (url) cfg.SUPABASE_URL = url;
    if (key) cfg.SUPABASE_ANON_KEY = key;
  } catch (e) {
    // localStorage indisponível — segue com valores do arquivo
  }

  window.PORTAL_CONFIG = cfg;
})();
