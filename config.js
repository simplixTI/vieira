// Configuração do portal. Preencha SUPABASE_ANON_KEY com a chave anon do projeto,
// ou use localStorage 'portal_supabase_url' / 'portal_anon_key' para testes sem editar o arquivo.
(function () {
  var cfg = {
    SUPABASE_URL: 'https://wipthjinvcyglbeuxxsb.supabase.co',
    SUPABASE_ANON_KEY: 'PREENCHER_ANON_KEY',
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
