#!/bin/bash
# Fetch a Heritrix distribution into ./heritrix and print its details as GitHub Actions outputs.
#
#   get-heritrix.sh release [VERSION]   a release's dist tarball (default: the latest release)
#   get-heritrix.sh dev                 built from the default branch
set -euo pipefail

repo=internetarchive/heritrix3
track=$1
version=${2:-}

if [ "$track" = release ]; then
  if [ -z "$version" ]; then
    version=$(gh release view -R "$repo" --json tagName -q .tagName)
  fi
  date=$(gh release view "$version" -R "$repo" --json publishedAt -q .publishedAt)
  revision=$(gh api "repos/$repo/commits/$version" -q .sha)
  # recent releases attach the distribution; older ones only published it to Maven Central
  gh release download "$version" -R "$repo" -p "heritrix-$version-dist.tar.gz" -O heritrix.tar.gz ||
    curl -fsSL -o heritrix.tar.gz \
      "https://repo1.maven.org/maven2/org/archive/heritrix/heritrix/$version/heritrix-$version-dist.tar.gz"
else
  git clone -q --depth 1 "https://github.com/$repo.git" heritrix-src
  revision=$(git -C heritrix-src rev-parse HEAD)
  date=$(git -C heritrix-src log -1 --format=%cI)
  (cd heritrix-src && mvn -B -ntp -q -DskipTests package)
  cp heritrix-src/dist/target/heritrix-*-dist.tar.gz heritrix.tar.gz
fi

mkdir heritrix
tar -xzf heritrix.tar.gz -C heritrix --strip-components 1
{
  echo "home=$PWD/heritrix"
  echo "revision=$revision"
  echo "date=$date"
} >> "$GITHUB_OUTPUT"
