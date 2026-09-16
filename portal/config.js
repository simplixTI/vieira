// Configuração do portal. Preencha SUPABASE_ANON_KEY com a chave anon do projeto,
// ou use localStorage 'portal_supabase_url' / 'portal_anon_key' para testes sem editar o arquivo.
(function () {
  var cfg = {
    SUPABASE_URL: 'https://wipthjinvcyglbeuxxsb.supabase.co',
    SUPABASE_ANON_KEY: 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6IndpcHRoamludmN5Z2xiZXV4eHNiIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODk1MjUxMzAsImV4cCI6MjEwNTEwMTEzMH0.s-IShdUxLV0o4tIpLutnpo9sMDivmQu34n1oqwmm0pA',
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
