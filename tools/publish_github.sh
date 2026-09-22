#!/usr/bin/env bash
# Публикация на GitHub: https://github.com/ALEXalesha/InstallerModels
#
#   bash tools/publish_github.sh            # только код
#   bash tools/publish_github.sh v2.0.0     # код и тег
#
# Источник правды - Gitea. На GitHub уезжает копия main, в которой личный gmail автора и
# коммитера заменён на анонимную почту аккаунта GitHub: почта автора видна любому, кто
# откроет коммит. Рабочая история не трогается, ветка пересобирается каждый раз заново -
# отсюда --force.
set -euo pipefail

export PATH="$PATH:/c/Program Files/GitHub CLI"
REPO=ALEXalesha/InstallerModels
# Личный адрес не записан здесь строкой: скрипт берёт его из настроек репозитория.
# Иначе выходило смешно - файл, который убирает почту автора из коммитов, публиковал
# её открытым текстом на первой же странице.
PRIVATE_EMAIL="$(git config user.email)"
PUBLIC_EMAIL=203467574+ALEXalesha@users.noreply.github.com
TAG="${1:-}"

cd "$(dirname "$0")/.."

git remote get-url github >/dev/null 2>&1 || git remote add github "https://github.com/$REPO.git"

git branch -f github-main main
FILTER_BRANCH_SQUELCH_WARNING=1 git filter-branch -f --env-filter "
  if [ \"\$GIT_AUTHOR_EMAIL\" = '$PRIVATE_EMAIL' ]; then export GIT_AUTHOR_EMAIL='$PUBLIC_EMAIL'; fi
  if [ \"\$GIT_COMMITTER_EMAIL\" = '$PRIVATE_EMAIL' ]; then export GIT_COMMITTER_EMAIL='$PUBLIC_EMAIL'; fi
" github-main >/dev/null 2>&1
git update-ref -d refs/original/refs/heads/github-main 2>/dev/null || true

# Личный адрес убирается и из содержимого файлов по всей публикуемой истории.
#
# Замена почты автора прячет её в метаданных коммита, но строка в файле остаётся на
# виду - именно так адрес и уехал на GitHub внутри этого самого скрипта. Правится
# только то, где он действительно встречается: блобы этих путей переписываются в
# индексе, без выгрузки дерева на диск (иначе LFS-репозиторий потянул бы все
# гигабайты весов на каждый коммит).
DIRTY=$(git grep -I -l -F "$PRIVATE_EMAIL" $(git rev-list github-main) -- 2>/dev/null \
        | sed 's/^[^:]*://' | sort -u | tr '\n' ' ')
if [ -n "$DIRTY" ]; then
  echo "личный адрес в файлах: $DIRTY- переписываю содержимое"
  FILTER_BRANCH_SQUELCH_WARNING=1 git filter-branch -f --index-filter "
    for f in $DIRTY; do
      mode=\$(git ls-tree \$GIT_COMMIT -- \"\$f\" | awk '{print \$1}')
      [ -n \"\$mode\" ] || continue
      blob=\$(git cat-file blob \$GIT_COMMIT:\"\$f\" \
             | sed 's/$PRIVATE_EMAIL/$PUBLIC_EMAIL/g' | git hash-object -w --stdin)
      git update-index --cacheinfo \$mode,\$blob,\"\$f\"
    done
  " github-main >/dev/null 2>&1
  git update-ref -d refs/original/refs/heads/github-main 2>/dev/null || true
fi

if git log github-main --format='%ae%n%ce' | grep -qx "$PRIVATE_EMAIL"; then
  echo "в публикуемой ветке остался личный адрес в авторе коммита - пуш отменён" >&2
  exit 1
fi

# И в самих файлах - по всей публикуемой истории, а не только в её вершине.
# Проверка появилась после того, как личный адрес уехал на GitHub открытым текстом
# внутри этого самого скрипта: почта автора в коммитах пряталась, а строка в файле
# лежала на виду.
if git grep -I -q -F "$PRIVATE_EMAIL" $(git rev-list github-main) -- 2>/dev/null; then
  echo "личный адрес остался в файлах публикуемой истории - пуш отменён" >&2
  git grep -I -l -F "$PRIVATE_EMAIL" $(git rev-list github-main) -- | head -5 >&2
  exit 1
fi

echo "ветка github-main: $(git rev-list --count github-main) коммитов, $(git log -1 --format=%h github-main)"
gh auth setup-git
git push github github-main:main --force

if [ -n "$TAG" ]; then
  git tag -f "$TAG" github-main
  git push github "refs/tags/$TAG" --force
  echo "тег $TAG отправлен"
fi
