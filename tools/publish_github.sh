#!/usr/bin/env bash
# Публикация на GitHub: https://github.com/ALEXalesha/InstallerModels
#
#   bash tools/publish_github.sh            # только код
#   bash tools/publish_github.sh vX.Y.Z     # код и тег
#
# Источник правды - Gitea. На GitHub уезжает копия main, пересобранная каждый раз
# заново (отсюда --force: в main на GitHub никто, кроме этого скрипта, не пишет).
# Рабочая история на Gitea не трогается.
#
# В публикуемой копии заменяется личное - и в авторах коммитов, и в содержимом
# файлов по всей истории:
#   - gmail автора   -> анонимная почта аккаунта GitHub;
#   - адрес Gitea    -> gitea.local;
#   - полное имя     -> ник ALEXalesha.
# Ни одна из этих строк не записана здесь: почта берётся из git config user.email,
# адрес - из remote origin, имя - из git config publish.privateName (локальная
# настройка, в историю не попадает). Строкой в скрипте личное однажды уже уехало на
# GitHub - внутри самого такого скрипта.
#
# Теги. Выпуски на GitHub привязаны к тегам, и после первой чистки истории старые
# теги остались на коммитах ДО неё: main был чистым, а почта оставалась видна через
# v2.0.0 и соседей. Поэтому каждый тег на GitHub, который не ведёт в свежую ветку,
# переставляется на её коммит с тем же временем и заголовком. Выпуск при этом
# остаётся на месте со всеми файлами. Новый тег ставится прямо на GitHub, без
# локального: локальный указывал бы на переписанный коммит и путался с тегами Gitea.
set -euo pipefail

export PATH="$PATH:/c/Program Files/GitHub CLI"
REPO=ALEXalesha/InstallerModels
PRIVATE_EMAIL="$(git config user.email)"
PUBLIC_EMAIL=203467574+ALEXalesha@users.noreply.github.com
LAN_GITEA="$(git remote get-url origin | sed -E 's#^[a-z]+://([^/:]+).*#\1#')"
PUBLIC_GITEA='gitea.local'
PRIVATE_NAME="$(git config --get publish.privateName || true)"
PUBLIC_NAME=ALEXalesha
TAG="${1:-}"

cd "$(dirname "$0")/.."

if [ -z "$PRIVATE_NAME" ]; then
  echo "не задано git config publish.privateName - без него полное имя не заменить" >&2
  exit 1
fi

# Для sed и grep -E точки в адресах надо экранировать, иначе они значат «любой символ».
esc() { printf '%s' "$1" | sed 's/[.[\*^$+?(){}|]/\\&/g'; }
E_EMAIL="$(esc "$PRIVATE_EMAIL")"
E_LAN="$(esc "$LAN_GITEA")"
E_NAME="$(esc "$PRIVATE_NAME")"
PATTERN="$E_EMAIL|$E_LAN|$E_NAME"

git remote get-url github >/dev/null 2>&1 || git remote add github "https://github.com/$REPO.git"

git branch -f github-main main
FILTER_BRANCH_SQUELCH_WARNING=1 git filter-branch -f --env-filter "
  if [ \"\$GIT_AUTHOR_EMAIL\" = '$PRIVATE_EMAIL' ]; then export GIT_AUTHOR_EMAIL='$PUBLIC_EMAIL'; fi
  if [ \"\$GIT_COMMITTER_EMAIL\" = '$PRIVATE_EMAIL' ]; then export GIT_COMMITTER_EMAIL='$PUBLIC_EMAIL'; fi
" github-main >/dev/null 2>&1
git update-ref -d refs/original/refs/heads/github-main 2>/dev/null || true

# `|| true`: без совпадений git grep возвращает 1, и при pipefail скрипт молча
# обрывался бы здесь.
DIRTY=$(git grep -I -l -E "$PATTERN" $(git rev-list github-main) -- 2>/dev/null \
        | sed 's/^[^:]*://' | sort -u | tr '\n' ' ' || true)
if [ -n "$DIRTY" ]; then
  echo "личное в файлах: $DIRTY- переписываю содержимое"
  FILTER_BRANCH_SQUELCH_WARNING=1 git filter-branch -f --index-filter "
    for f in $DIRTY; do
      mode=\$(git ls-tree \$GIT_COMMIT -- \"\$f\" | awk '{print \$1}')
      [ -n \"\$mode\" ] || continue
      blob=\$(git cat-file blob \$GIT_COMMIT:\"\$f\" \
             | sed -e 's/$E_EMAIL/$PUBLIC_EMAIL/g' -e 's/$E_LAN/$PUBLIC_GITEA/g' \
                   -e 's/$E_NAME/$PUBLIC_NAME/g' \
             | git hash-object -w --stdin)
      git update-index --cacheinfo \$mode,\$blob,\"\$f\"
    done
  " github-main >/dev/null 2>&1
  git update-ref -d refs/original/refs/heads/github-main 2>/dev/null || true
fi

if git log github-main --format='%ae%n%ce%n%an%n%cn' | grep -q -E -x "$PATTERN"; then
  echo "в публикуемой ветке осталось личное в авторе коммита - пуш отменён" >&2
  exit 1
fi
if git grep -I -q -E "$PATTERN" $(git rev-list github-main) -- 2>/dev/null; then
  echo "личное осталось в файлах публикуемой истории - пуш отменён" >&2
  exit 1
fi

# Теги на GitHub, которые не ведут в свежую ветку, получают пару по времени и заголовку.
MOVES=""
git for-each-ref --format='%(refname)' refs/github-tags | xargs -r -n1 git update-ref -d
git fetch -q github '+refs/tags/*:refs/github-tags/*' 2>/dev/null || true
for ref in $(git for-each-ref --format='%(refname)' refs/github-tags); do
  name=${ref#refs/github-tags/}
  old=$(git rev-parse "$ref^{commit}")
  git merge-base --is-ancestor "$old" github-main && continue
  key=$(git log -1 --format='%at %s' "$old")
  # awk дочитывает вход до конца, без exit: при раннем выходе git log получал SIGPIPE,
  # и с pipefail скрипт обрывался здесь молча (код 141) - так было с двумя репозиториями.
  new=$(git log github-main --format='%H %at %s' \
        | awk -v k="$key" '{h=$1; $1=""; if (!f && substr($0,2)==k) {print h; f=1}}')
  if [ -z "$new" ]; then
    echo "тег $name: в публикуемой ветке нет коммита с тем же временем и заголовком - оставлен" >&2
    continue
  fi
  echo "тег $name: $(git rev-parse --short "$old") -> $(git rev-parse --short "$new")"
  MOVES="$MOVES +$new:refs/tags/$name"
done
git for-each-ref --format='%(refname)' refs/github-tags | xargs -r -n1 git update-ref -d

echo "ветка github-main: $(git rev-list --count github-main) коммитов, $(git log -1 --format=%h github-main)"
gh auth setup-git
git push github github-main:main --force
if [ -n "$MOVES" ]; then
  git push github $MOVES
fi

if [ -n "$TAG" ]; then
  git push github "+github-main:refs/tags/$TAG"
  echo "тег $TAG отправлен"
fi
