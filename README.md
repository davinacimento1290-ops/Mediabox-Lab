# MEDIABOX — Video Hub

Repositório público de desenvolvimento do MEDIABOX.

## Estrutura
- `main.py`: aplicação web Flask.
- `mediabox_captcha.py`: verificação anti-bot.
- `admin_control.py`: painel de administração.
- `app/`: projeto Android nativo.
- `.github/workflows/build.yml`: automação de compilação Android.

## Configuração segura
Não publique nem adicione senhas, tokens, cookies, chaves de API ou arquivos de sessão.
Configure credenciais e chaves somente nas variáveis privadas do serviço de hospedagem.

Variáveis usadas pelo projeto incluem:
- `MEDIABOX_HOSTED=true`
- `MEDIABOX_USERNAME`
- `MEDIABOX_PASSWORD_HASH`
- `MEDIABOX_SECRET_KEY`
- `YOUTUBE_API_KEY`

**Atenção:** antes de implantar uma cópia pública, configure as variáveis privadas e revise as permissões e os arquivos versionados. O modo local legado possui credenciais padrão de desenvolvimento; não o utilize em um servidor público sem configurar as variáveis de autenticação.

## Build Android
O workflow em `.github/workflows/build.yml` compila o APK de depuração por GitHub Actions.
