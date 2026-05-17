# /etc/fish/conf.d/sudo-telegram.fish
# Loaded automatically by every fish session (login or not).
# Equivalent of /etc/profile.d/sudo-telegram.sh for bash/zsh.

set -gx SUDO_ASKPASS /usr/local/bin/sudo-telegram-askpass
alias sudo='sudo -A'
