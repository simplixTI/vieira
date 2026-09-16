# Portal de Verificação Eleitoral

SPA estática (sem build, sem npm) para clientes enviarem listas de CPF e acompanharem
a verificação de elegibilidade junto ao TSE. Backend: Supabase (tabelas `batches` e `voter_records`).

## Configuração da chave do Supabase

1. **Opção A — editar o arquivo:** preencha a chave anon em `config.js`:

   ```js
   SUPABASE_ANON_KEY: 'eyJhbGciOi...'   // chave anon do projeto
   ```

2. **Opção B — localStorage (sem editar arquivos, útil para testes):** no console do navegador:

   ```js
   localStorage.setItem('portal_supabase_url', 'https://wipthjinvcyglbeuxxsb.supabase.co');
   localStorage.setItem('portal_anon_key', 'eyJhbGciOi...');
   ```

Enquanto a chave estiver como `PREENCHER_ANON_KEY`, o portal exibe um aviso de configuração incompleta.

## Rodando localmente

Basta servir a pasta como estático (qualquer servidor funciona):

```bash
cd portal
python -m http.server 8080
# ou: npx serve .
```

Abra http://localhost:8080.

## Deploy na Vercel

- **Drag-and-drop:** acesse https://vercel.com/new, arraste a pasta `portal/` e publique.
- **CLI:** `npm i -g vercel` e, dentro de `portal/`, rode `vercel --prod`.

É um site 100% estático — nenhuma configuração de build é necessária (output = pasta raiz).

## Cadastro de usuários

O **cadastro público está desativado**. As contas dos clientes devem ser criadas pela
administração em **Supabase Dashboard → Authentication → Users → Add user**. O portal só
aceita login com e-mail e senha já cadastrados.
