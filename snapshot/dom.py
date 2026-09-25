"""Resource snapshot: dom."""

from __future__ import annotations

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError


def _dismiss_common_overlays(page) -> None:
    for _ in range(2):
        try:
            page.keyboard.press("Escape")
        except Exception:
            break

    page.evaluate(
        """
        () => {
          const isVisible = (node) => {
            if (!(node instanceof HTMLElement)) {
              return false;
            }
            const style = window.getComputedStyle(node);
            if (!style) {
              return false;
            }
            if (style.display === 'none' || style.visibility === 'hidden') {
              return false;
            }
            const rect = node.getBoundingClientRect();
            return rect.width > 20 && rect.height > 20;
          };

          const selectors = [
            '[role="dialog"]',
            '[data-testid="sheetDialog"]',
            '[data-testid="BottomBar"]',
            '[data-testid="DMDrawer"]'
          ];
          for (const selector of selectors) {
            document.querySelectorAll(selector).forEach((node) => node.remove());
          }

          const primaryColumn = document.querySelector('[data-testid="primaryColumn"]');
          if (primaryColumn instanceof HTMLElement) {
            for (const node of primaryColumn.querySelectorAll('*')) {
              if (!(node instanceof HTMLElement) || !isVisible(node)) {
                continue;
              }

              const style = window.getComputedStyle(node);
              const rect = node.getBoundingClientRect();
              const text = (node.innerText || '').trim();

              const isTopStickyBar =
                (style.position === 'sticky' || style.position === 'fixed') &&
                rect.top <= 1 &&
                rect.height <= 80;

              const isTopFeedNotice =
                text &&
                ['查看新帖子', 'View new posts', 'See new posts'].includes(text) &&
                rect.top < 140 &&
                rect.height <= 80;

              if (isTopStickyBar || isTopFeedNotice) {
                node.remove();
              }
            }
          }

          document.documentElement.style.scrollBehavior = 'auto';
          document.body.style.overflow = 'auto';
        }
        """
    )


def _expand_tweet_text(tweet_card) -> None:
    try:
        tweet_card.evaluate(
            """
            (root) => {
              const clickables = [
                ...root.querySelectorAll('[data-testid="tweet-text-show-more-link"]'),
                ...root.querySelectorAll('a, button, [role="button"], span'),
              ];
              for (const node of clickables) {
                const text = (node.textContent || '').trim();
                if (!text || text.length > 24) {
                  continue;
                }
                if (/show more|显示更多|展开全文|顯示更多|もっと見る|さらに表示/i.test(text)) {
                  node.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                }
              }

              for (const node of root.querySelectorAll('[data-testid="tweetText"], div[dir="auto"]')) {
                if (!(node instanceof HTMLElement)) {
                  continue;
                }
                node.style.setProperty('overflow', 'visible', 'important');
                node.style.setProperty('max-height', 'none', 'important');
                node.style.setProperty('-webkit-line-clamp', 'unset', 'important');
                node.style.setProperty('line-clamp', 'unset', 'important');
                node.style.setProperty('display', 'block', 'important');
              }
            }
            """
        )
    except Exception:
        pass


def _hide_non_primary_columns(page, tweet_id: str | None = None) -> None:
    try:
        page.evaluate(
            """
            (tweetId) => {
              const removeNode = (node) => {
                if (node instanceof Element) {
                  node.remove();
                }
              };

              const hideNode = (node) => {
                if (node instanceof HTMLElement) {
                  node.style.setProperty('display', 'none', 'important');
                  node.style.setProperty('visibility', 'hidden', 'important');
                  node.style.setProperty('pointer-events', 'none', 'important');
                  node.style.setProperty('height', '0', 'important');
                  node.style.setProperty('max-height', '0', 'important');
                  node.style.setProperty('overflow', 'hidden', 'important');
                  node.style.setProperty('margin', '0', 'important');
                  node.style.setProperty('padding', '0', 'important');
                }
              };

              const articleMatchesTweet = (article) => {
                if (!(article instanceof Element)) {
                  return false;
                }
                if (!tweetId) {
                  return true;
                }
                if (article.getAttribute('data-tweet-id') === tweetId) {
                  return true;
                }
                if (article.querySelector(`a[href*="/status/${tweetId}"]`)) {
                  return true;
                }
                return false;
              };

              const selectors = [
                '[data-testid="sidebarColumn"]',
                '[data-testid="secondaryColumn"]',
                '[data-testid="BottomBar"]',
                '[data-testid="DMDrawer"]',
                'header[role="banner"]',
              ];
              for (const selector of selectors) {
                document.querySelectorAll(selector).forEach(removeNode);
              }

              const primary = document.querySelector('[data-testid="primaryColumn"]');
              if (primary?.parentElement) {
                for (const child of [...primary.parentElement.children]) {
                  if (child !== primary) {
                    removeNode(child);
                  }
                }
              }

              const main = document.querySelector('main[role="main"]');
              if (main) {
                const children = [...main.children].filter((node) => node instanceof HTMLElement);
                if (children.length > 1) {
                  const primaryChild =
                    children.find((node) => node.querySelector('article[data-tweet-id], article[data-testid="tweet"]')) ||
                    children[0];
                  for (const child of children) {
                    if (child !== primaryChild) {
                      removeNode(child);
                    }
                  }
                }
              }

              // Remove / collapse every non-target tweet cell so replies cannot peek
              // into the screenshot after viewport resizes reflow the timeline.
              const articles = [...document.querySelectorAll(
                'article[data-tweet-id], article[data-testid="tweet"]'
              )];
              let targetArticle =
                articles.find((article) => articleMatchesTweet(article)) || articles[0] || null;

              for (const article of articles) {
                if (article === targetArticle) {
                  continue;
                }
                if (targetArticle?.contains(article)) {
                  continue;
                }
                const cell = article.closest('[data-testid="cellInnerDiv"]') || article;
                removeNode(cell);
              }

              if (targetArticle) {
                const targetCell = targetArticle.closest('[data-testid="cellInnerDiv"]');
                if (targetCell?.parentElement) {
                  for (const sibling of [...targetCell.parentElement.children]) {
                    if (sibling !== targetCell && sibling instanceof HTMLElement) {
                      // Keep structural spacers out of the capture by removing them.
                      if (
                        sibling.querySelector(
                          'article[data-tweet-id], article[data-testid="tweet"], [data-testid="User-Name"]',
                        ) ||
                        (sibling.textContent || '').trim().length > 0
                      ) {
                        removeNode(sibling);
                      } else {
                        hideNode(sibling);
                      }
                    }
                  }
                }

                // Also collapse siblings of any ancestor cell wrapper
                let current = targetCell;
                while (current?.parentElement) {
                  const parent = current.parentElement;
                  if (parent === main || parent === primary || parent === document.body) break;
                  for (const sibling of [...parent.children]) {
                    if (sibling !== current && sibling instanceof HTMLElement) {
                      if (
                        sibling.querySelector(
                          'article[data-tweet-id], article[data-testid="tweet"], [data-testid="User-Name"]',
                        )
                      ) {
                        removeNode(sibling);
                      } else {
                        hideNode(sibling);
                      }
                    }
                  }
                  current = parent;
                }
              }

              // Hide any remaining peek / suggestion / composer elements
              const peekSelectors = [
                '[data-testid="tweet-detail-more-replies"]',
                '[data-testid="conversation-more-replies"]',
                '[data-testid="related-tweets"]',
                '[data-testid="inline_reply_offscreen"]',
                '[data-testid="logged_out_read_replies_pivot"]',
                '[data-testid="inline_reply_composer"]',
                '[data-testid="tweetTextarea_0"]',
                '[data-testid="tweetButtonInline"]',
                'form[aria-label*="Reply"]',
                'form[aria-label*="reply"]',
                'form[aria-label*="回复"]',
                'form[aria-label*="回覆"]',
                'output',
              ];
              for (const sel of peekSelectors) {
                document.querySelectorAll(sel).forEach(removeNode);
              }

              // Drop every timeline cell after the target tweet (reply composer
              // skeletons, "only some accounts can reply", next tweets, etc.).
              if (targetArticle) {
                const targetCell =
                  targetArticle.closest('[data-testid="cellInnerDiv"]') || targetArticle;
                let sibling = targetCell.nextElementSibling;
                while (sibling) {
                  const next = sibling.nextElementSibling;
                  removeNode(sibling);
                  sibling = next;
                }
                // Also clear later siblings of ancestor section wrappers.
                let current = targetCell.parentElement;
                while (current && current !== main && current !== primary && current !== document.body) {
                  sibling = current.nextElementSibling;
                  while (sibling) {
                    const next = sibling.nextElementSibling;
                    removeNode(sibling);
                    sibling = next;
                  }
                  current = current.parentElement;
                }
              }
            }
            """,
            tweet_id,
        )
    except Exception:
        pass


def _wait_for_tweet_card(page, tweet_id: str, timeout_ms: int):
    """Wait for all supported card shapes together, with one timeout budget."""
    if not str(tweet_id).isdigit():
        raise ValueError("Invalid tweet ID")
    # Match ID boundaries so a quote/link to status 1234 cannot match status 123.
    links = ", ".join(
        f'a[href{operator}="/status/{tweet_id}{suffix}"]'
        for operator, suffix in (("$", ""), ("*", "?"), ("*", "#"), ("*", "/"))
    )
    card = page.locator(
        f'article[data-tweet-id="{tweet_id}"]:visible, '
        f'article:visible:has({links})'
    ).first
    try:
        card.wait_for(state="visible", timeout=timeout_ms)
        return card
    except PlaywrightTimeoutError:
        return None


def _scroll_tweet_into_view(page, tweet_card, *, guest_mode: bool = False) -> None:
    tweet_card.scroll_into_view_if_needed(timeout=10000)

    box = tweet_card.bounding_box()
    if not box:
        return

    top_padding = 16 if guest_mode else 120
    target_top = max(int(box["y"] - top_padding), 0)
    page.evaluate("(top) => window.scrollTo(0, top)", target_top)
    # Layout only. Image and video readiness is checked separately.
    page.wait_for_timeout(80)


def _wait_for_tweet_assets(page, tweet_card) -> None:
    element = tweet_card.element_handle(timeout=5000)
    if element is None:
        return

    try:
        page.wait_for_function(
            """
            (el) => {
              const images = [...el.querySelectorAll(
                '[data-testid="tweetPhoto"] img, [data-testid="card.layoutLarge.media"] img, [data-testid="card.layoutSmall.media"] img, .media-cell img'
              )].filter((img) => {
                if (!(img instanceof HTMLImageElement) || img.offsetParent === null) {
                  return false;
                }
                return !img.closest('[data-testid="Tweet-User-Avatar"], [data-testid^="UserAvatar"]');
              });
              return images.length === 0 || images.every(
                (img) => img.complete && img.naturalWidth > 0
              );
            }
            """,
            arg=element,
            timeout=8000,
        )
    except PlaywrightTimeoutError:
        pass

    try:
        page.wait_for_function(
            """
            (el) => {
              const busyNodes = [...el.querySelectorAll('[aria-busy="true"], [role="progressbar"]')]
                .filter((node) => {
                  if (node.offsetParent === null) {
                    return false;
                  }
                  // Video scrubbers stay visible for the whole clip and are not a load gate.
                  return !node.closest(
                    'video, [data-testid="videoComponent"], [data-testid="videoPlayer"]'
                  );
                });
              return busyNodes.length === 0;
            }
            """,
            arg=element,
            timeout=4000,
        )
    except PlaywrightTimeoutError:
        pass

    try:
        page.wait_for_function(
            """
            (el) => {
              const videos = [...el.querySelectorAll('video')].filter((video) => {
                const style = window.getComputedStyle(video);
                if (!style) {
                  return false;
                }
                if (style.display === 'none' || style.visibility === 'hidden') {
                  return false;
                }
                const rect = video.getBoundingClientRect();
                return rect.width >= 48 && rect.height >= 48;
              });
              return videos.length === 0 || videos.every((video) => video.readyState >= 1);
            }
            """,
            arg=element,
            timeout=6000,
        )
    except PlaywrightTimeoutError:
        pass

    page.wait_for_timeout(80)


def _wait_for_public_fallback_assets(page, tweet_card) -> None:
    element = tweet_card.element_handle(timeout=3000)
    if element is None:
        return

    try:
        page.wait_for_function(
            """
            (el) => {
              const images = [...el.querySelectorAll('img')]
                .filter((img) => img.offsetParent !== null);
              return images.length === 0 || images.every(
                (img) => img.complete && img.naturalWidth > 0
              );
            }
            """,
            arg=element,
            timeout=2500,
        )
    except PlaywrightTimeoutError:
        pass

    try:
        page.wait_for_function(
            """
            (el) => {
              const videos = [...el.querySelectorAll('video')].filter((video) => {
                const src = video.currentSrc || video.src || video.getAttribute('src') || '';
                return Boolean(src);
              });
              return videos.length === 0 || videos.every(
                (video) => video.readyState >= 1 || video.error
              );
            }
            """,
            arg=element,
            timeout=8000,
        )
    except PlaywrightTimeoutError:
        pass

    page.wait_for_timeout(120)
