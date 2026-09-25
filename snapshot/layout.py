"""Resource snapshot: layout."""

from __future__ import annotations

from pathlib import Path
from .config import (
    CAPTURE_VIEWPORT_MARGIN,
    DEFAULT_VIEWPORT_HEIGHT,
    DEFAULT_VIEWPORT_WIDTH,
    TRANSLATION_ATTR,
)
from .dom import (
    _hide_non_primary_columns,
    _scroll_tweet_into_view,
)


def _prepare_tweet_media_for_screenshot(tweet_card) -> None:
    try:
        tweet_card.evaluate(
            """
            (root) => {
              const GRID_ATTR = 'data-resource-snapshot-media-grid';

              const isAvatarImage = (img) => {
                if (!(img instanceof HTMLImageElement)) {
                  return false;
                }
                return Boolean(
                  img.closest(
                    '[data-testid="Tweet-User-Avatar"], [data-testid="UserAvatar-Container-Unknown"], [data-testid="User-Name"]',
                  ),
                );
              };

              const isMediaImage = (img) => {
                if (!(img instanceof HTMLImageElement)) {
                  return false;
                }
                if (isAvatarImage(img)) {
                  return false;
                }
                return img.naturalWidth > 80;
              };

              const removeNode = (node) => {
                if (node instanceof Element) {
                  node.remove();
                }
              };

              const hasRenderableImage = (node) =>
                Boolean(
                  [...node.querySelectorAll('img')].some((img) => isMediaImage(img)),
                );

              const hasRenderableVideo = (node) =>
                Boolean(
                  [...node.querySelectorAll('video')].some((video) => {
                    if (!(video instanceof HTMLVideoElement)) {
                      return false;
                    }
                    const src = video.currentSrc || video.src || video.getAttribute('src') || '';
                    const poster = video.poster || video.getAttribute('poster') || '';
                    return Boolean(src || poster || video.videoWidth > 1 || video.videoHeight > 1);
                  }),
                );

              for (const mediaRoot of [
                ...root.querySelectorAll('[data-testid="tweetPhoto"], [data-testid="videoComponent"], [data-testid="videoPlayer"]'),
              ]) {
                if (!(mediaRoot instanceof HTMLElement)) {
                  continue;
                }
                if (mediaRoot.closest(`[${GRID_ATTR}]`)) {
                  continue;
                }
                if (!hasRenderableImage(mediaRoot) && !hasRenderableVideo(mediaRoot)) {
                  removeNode(mediaRoot);
                }
              }

              for (const btn of root.querySelectorAll('button, [role="button"]')) {
                const label = (btn.getAttribute('aria-label') || btn.textContent || '').trim();
                if (/^(next|previous|上一|下一|次|前)/i.test(label)) {
                  removeNode(btn);
                }
              }

              root.querySelectorAll(`[${GRID_ATTR}]`).forEach(removeNode);

              // Detect and fix multi-cam video layouts (multiple simultaneous video panels)
              const allVideos = [...root.querySelectorAll('video')];
              if (allVideos.length > 1) {
                // Find the common ancestor container that holds all video panels
                const videoContainers = new Map();
                for (const video of allVideos) {
                  let parent = video.parentElement;
                  while (parent && parent !== root) {
                    const videos = parent.querySelectorAll('video');
                    if (videos.length > 1) {
                      const key = parent;
                      if (!videoContainers.has(key)) {
                        videoContainers.set(key, videos.length);
                      }
                      break;
                    }
                    parent = parent.parentElement;
                  }
                }

                // Find the innermost container with multiple videos
                let multiCamContainer = null;
                let minVideoCount = Infinity;
                for (const [container, count] of videoContainers) {
                  if (count > 1 && count < minVideoCount) {
                    multiCamContainer = container;
                    minVideoCount = count;
                  }
                }

                if (multiCamContainer instanceof HTMLElement) {
                  // Ensure the multi-cam container fits within the article
                  multiCamContainer.style.overflow = 'hidden';
                  multiCamContainer.style.width = '100%';
                  multiCamContainer.style.maxWidth = '100%';

                  // Also ensure all ancestor containers don't overflow
                  let ancestor = multiCamContainer.parentElement;
                  while (ancestor instanceof HTMLElement && ancestor !== root) {
                    ancestor.style.overflow = 'hidden';
                    ancestor.style.maxWidth = '100%';
                    ancestor = ancestor.parentElement;
                  }
                }
              }

              const carousels = [...root.querySelectorAll('div')].filter((node) => {
                if (!(node instanceof HTMLElement)) {
                  return false;
                }
                const cls = typeof node.className === 'string' ? node.className : '';
                return cls.includes('snap-x') && cls.includes('snap-mandatory');
              });

              const upgradeTwimgUrl = (url, name = 'large') => {
                if (!url || typeof url !== 'string') {
                  return url;
                }
                if (!/pbs\\.twimg\\.com\\/media\\//i.test(url)) {
                  return url;
                }
                if (/[?&]name=/.test(url)) {
                  return url.replace(/([?&]name=)[^&]*/i, `$1${name}`);
                }
                return `${url}${url.includes('?') ? '&' : '?'}name=${name}`;
              };

              const buildGridCell = (img) => {
                const cell = document.createElement('div');
                cell.style.position = 'relative';
                cell.style.overflow = 'hidden';
                cell.style.minWidth = '0';
                cell.style.minHeight = '0';
                cell.style.width = '100%';
                cell.style.height = '100%';

                const clone = img.cloneNode(true);
                clone.removeAttribute('style');
                clone.className = '';
                if (clone instanceof HTMLImageElement) {
                  const upgraded = upgradeTwimgUrl(clone.currentSrc || clone.src, 'large');
                  if (upgraded) {
                    clone.src = upgraded;
                  }
                  clone.removeAttribute('srcset');
                  clone.sizes = '100vw';
                }
                clone.style.width = '100%';
                clone.style.height = '100%';
                clone.style.objectFit = 'cover';
                clone.style.objectPosition = 'center center';
                clone.style.display = 'block';
                cell.appendChild(clone);
                return cell;
              };

              const buildVideoGridCell = (video) => {
                const cell = document.createElement('div');
                cell.style.position = 'relative';
                cell.style.overflow = 'hidden';
                cell.style.minWidth = '0';
                cell.style.minHeight = '0';
                cell.style.width = '100%';
                cell.style.height = '100%';
                cell.style.background = '#000';

                const canvas = document.createElement('canvas');
                const width = video.videoWidth || Math.max(1, Math.round(video.getBoundingClientRect().width));
                const height = video.videoHeight || Math.max(1, Math.round(video.getBoundingClientRect().height));
                canvas.width = width;
                canvas.height = height;
                let drewFrame = false;
                try {
                  const context = canvas.getContext('2d');
                  if (context && video.videoWidth > 1 && video.videoHeight > 1 && video.readyState >= 2) {
                    context.drawImage(video, 0, 0, width, height);
                    drewFrame = true;
                  }
                } catch (error) {
                }
                if (drewFrame) {
                  canvas.style.width = '100%';
                  canvas.style.height = '100%';
                  // The player may be square even when the decoded frame is
                  // portrait. Preserve every source pixel instead of cropping
                  // the frame to fill the player's aspect ratio.
                  canvas.style.objectFit = 'contain';
                  canvas.style.objectPosition = 'center center';
                  canvas.style.display = 'block';
                  cell.appendChild(canvas);
                } else if (video.poster) {
                  const poster = document.createElement('img');
                  poster.src = video.poster;
                  poster.style.width = '100%';
                  poster.style.height = '100%';
                  poster.style.objectFit = 'cover';
                  poster.style.objectPosition = 'center center';
                  poster.style.display = 'block';
                  cell.appendChild(poster);
                }
                return cell;
              };

              // Carousel parents keep a short landscape frame. After we rebuild a
              // taller 2x2 grid, unlock those ancestors or the bottom row gets
              // clipped into "forehead-only" peeks.
              const unlockMediaAncestors = (mediaEl) => {
                if (!(mediaEl instanceof HTMLElement)) {
                  return;
                }
                let node = mediaEl.parentElement;
                while (node instanceof HTMLElement && node !== root) {
                  node.style.height = 'auto';
                  node.style.maxHeight = 'none';
                  node.style.minHeight = '0';
                  node.style.aspectRatio = 'auto';
                  node.style.flex = '0 0 auto';
                  node.style.maxWidth = '100%';
                  node.style.width = '100%';
                  if (node !== mediaEl) {
                    const overflowY = window.getComputedStyle(node).overflowY;
                    if (overflowY === 'hidden' || overflowY === 'clip') {
                      node.style.overflow = 'visible';
                      node.style.overflowY = 'visible';
                    }
                  }
                  node = node.parentElement;
                }
              };

              const mountPhotoGrid = (carousel, grid, imageCount) => {
                if (imageCount <= 1) {
                  return;
                }
                if (imageCount === 2) {
                  grid.style.aspectRatio = '16 / 9';
                } else if (imageCount === 3) {
                  grid.style.aspectRatio = '4 / 3';
                } else {
                  // Match X's 4-photo mosaic more closely than a perfect square.
                  grid.style.aspectRatio = '7 / 8';
                }
                grid.style.height = 'auto';
                grid.style.minHeight = '0';
                grid.style.maxHeight = 'none';
                carousel.replaceWith(grid);
                unlockMediaAncestors(grid);

                // Force a concrete height from the final width so 1fr rows cannot
                // collapse inside a still-constrained parent during layout.
                const width = grid.getBoundingClientRect().width;
                if (width > 1) {
                  const ratio = imageCount === 2 ? 9 / 16 : imageCount === 3 ? 3 / 4 : 8 / 7;
                  const height = Math.round(width * ratio);
                  grid.style.height = `${height}px`;
                  grid.style.aspectRatio = 'auto';
                }
              };

              const cellFromMediaEntry = (entry) => {
                if (entry.type === 'video') {
                  const video = entry.node instanceof HTMLVideoElement
                    ? entry.node
                    : (entry.node instanceof Element ? entry.node.querySelector('video') : null);
                  if (video instanceof HTMLVideoElement) {
                    return buildVideoGridCell(video);
                  }
                  const poster = entry.node instanceof Element
                    ? [...entry.node.querySelectorAll('img')].find((img) => isMediaImage(img))
                    : null;
                  if (poster) {
                    return buildGridCell(poster);
                  }
                  const empty = document.createElement('div');
                  empty.style.background = '#000';
                  empty.style.width = '100%';
                  empty.style.height = '100%';
                  return empty;
                }
                return buildGridCell(entry.node);
              };

              const buildMixedMediaGrid = (entries) => {
                const grid = document.createElement('div');
                grid.setAttribute(GRID_ATTR, 'true');
                grid.style.display = 'grid';
                grid.style.width = '100%';
                grid.style.gap = '2px';
                grid.style.borderRadius = '16px';
                grid.style.overflow = 'hidden';
                grid.style.background = '#000';
                const count = Math.min(entries.length, 4);
                if (count === 2) {
                  grid.style.gridTemplateColumns = '1fr 1fr';
                  grid.style.gridTemplateRows = '1fr';
                  grid.style.aspectRatio = '16 / 9';
                } else if (count === 3) {
                  grid.style.gridTemplateColumns = '1fr 1fr';
                  grid.style.gridTemplateRows = '1fr 1fr';
                  grid.style.aspectRatio = '4 / 3';
                } else {
                  grid.style.gridTemplateColumns = '1fr 1fr';
                  grid.style.gridTemplateRows = '1fr 1fr';
                  grid.style.aspectRatio = '1 / 1';
                }
                entries.slice(0, 4).forEach((entry, index) => {
                  const cell = cellFromMediaEntry(entry);
                  if (count === 3 && index === 0) {
                    cell.style.gridRow = '1 / span 2';
                    cell.style.gridColumn = '1';
                  } else if (count === 3 && index === 1) {
                    cell.style.gridRow = '1';
                    cell.style.gridColumn = '2';
                  } else if (count === 3 && index === 2) {
                    cell.style.gridRow = '2';
                    cell.style.gridColumn = '2';
                  }
                  grid.appendChild(cell);
                });
                return grid;
              };

              for (const carousel of carousels) {
                const slides = [...carousel.children].filter((child) => child.querySelector('img'));
                const images = slides
                  .map((slide) => slide.querySelector('img'))
                  .filter((img) => isMediaImage(img));
                const videoSlides = [...carousel.children].filter(
                  (child) => child.querySelector('video') || child.querySelector('[data-testid="videoComponent"]')
                );

                if (images.length > 0 && videoSlides.length > 0) {
                  const mediaSlides = [...carousel.children]
                    .map((slide) => {
                      const video = slide.querySelector('video');
                      if (video) {
                        return { type: 'video', node: video };
                      }
                      const img = [...slide.querySelectorAll('img')].find((candidate) => isMediaImage(candidate));
                      if (img) {
                        return { type: 'image', node: img };
                      }
                      return null;
                    })
                    .filter(Boolean)
                    .slice(0, 4);
                  if (mediaSlides.length > 1) {
                    const grid = buildMixedMediaGrid(mediaSlides);
                    mountPhotoGrid(carousel, grid, mediaSlides.length);
                    continue;
                  }
                }

                if (images.length === 0) {
                  // Handle video carousels
                  if (videoSlides.length > 1) {
                    // Multi-cam: convert horizontal carousel to grid layout
                    const grid = document.createElement('div');
                    grid.style.display = 'grid';
                    grid.style.width = '100%';
                    grid.style.gap = '2px';
                    grid.style.borderRadius = '16px';
                    grid.style.overflow = 'hidden';
                    grid.style.background = '#000';
                    if (videoSlides.length === 2) {
                      grid.style.gridTemplateColumns = '1fr 1fr';
                      grid.style.gridTemplateRows = '1fr';
                      grid.style.aspectRatio = '16 / 9';
                    } else if (videoSlides.length === 3) {
                      grid.style.gridTemplateColumns = '3fr 2fr';
                      grid.style.gridTemplateRows = '1fr 1fr';
                      grid.style.aspectRatio = '16 / 10';
                    } else {
                      grid.style.gridTemplateColumns = '1fr 1fr';
                      grid.style.gridTemplateRows = '1fr 1fr';
                      grid.style.aspectRatio = '1 / 1';
                    }
                    for (let i = 0; i < Math.min(videoSlides.length, 4); i++) {
                      const cell = document.createElement('div');
                      cell.style.position = 'relative';
                      cell.style.overflow = 'hidden';
                      cell.style.minWidth = '0';
                      cell.style.minHeight = '0';
                      cell.style.width = '100%';
                      cell.style.height = '100%';
                      cell.style.background = '#000';
                      const slide = videoSlides[i];
                      const video = slide.querySelector('video');
                      if (video) {
                        const clone = video.cloneNode(true);
                        clone.removeAttribute('style');
                        clone.className = '';
                        clone.style.width = '100%';
                        clone.style.height = '100%';
                        clone.style.objectFit = 'cover';
                        clone.style.objectPosition = 'top';
                        clone.style.display = 'block';
                        clone.muted = true;
                        clone.defaultMuted = true;
                        clone.playsInline = true;
                        clone.controls = false;
                        clone.autoplay = false;
                        clone.currentTime = video.currentTime || 0;
                        cell.appendChild(clone);
                      } else {
                        const inner = slide.cloneNode(true);
                        inner.style.width = '100%';
                        inner.style.height = '100%';
                        cell.appendChild(inner);
                      }
                      if (videoSlides.length === 3 && i === 0) {
                        cell.style.gridRow = '1 / span 2';
                      }
                      grid.appendChild(cell);
                    }
                    carousel.replaceWith(grid);
                  } else if (videoSlides.length === 1) {
                    carousel.style.display = 'block';
                    carousel.style.overflow = 'hidden';
                    carousel.style.width = '100%';
                    carousel.style.borderRadius = '16px';
                    const slide = videoSlides[0];
                    slide.style.width = '100%';
                    slide.style.maxWidth = '100%';
                    slide.style.flexShrink = '0';
                  }
                  continue;
                }

                if (images.length === 1) {
                  carousel.style.display = 'block';
                  carousel.style.overflow = 'hidden';
                  carousel.style.width = '100%';
                  carousel.style.borderRadius = '16px';
                  for (const slide of slides) {
                    slide.style.width = '100%';
                    slide.style.maxWidth = '100%';
                    slide.style.flexShrink = '0';
                  }
                  const img = images[0];
                  img.style.width = '100%';
                  img.style.height = 'auto';
                  img.style.objectFit = 'cover';
                  img.style.display = 'block';
                  img.style.borderRadius = '16px';
                  continue;
                }

                const grid = document.createElement('div');
                grid.setAttribute(GRID_ATTR, 'true');
                grid.style.display = 'grid';
                grid.style.width = '100%';
                grid.style.gap = '2px';
                grid.style.borderRadius = '16px';
                grid.style.overflow = 'hidden';
                grid.style.background = '#000';

                if (images.length === 2) {
                  grid.style.gridTemplateColumns = '1fr 1fr';
                  grid.style.gridTemplateRows = '1fr';
                  for (const img of images.slice(0, 2)) {
                    grid.appendChild(buildGridCell(img));
                  }
                } else if (images.length === 3) {
                  grid.style.gridTemplateColumns = '1fr 1fr';
                  grid.style.gridTemplateRows = '1fr 1fr';
                  const cells = images.slice(0, 3).map((img) => buildGridCell(img));
                  cells[0].style.gridRow = '1 / span 2';
                  cells[0].style.gridColumn = '1';
                  cells[1].style.gridRow = '1';
                  cells[1].style.gridColumn = '2';
                  cells[2].style.gridRow = '2';
                  cells[2].style.gridColumn = '2';
                  for (const cell of cells) {
                    grid.appendChild(cell);
                  }
                } else {
                  grid.style.gridTemplateColumns = '1fr 1fr';
                  grid.style.gridTemplateRows = '1fr 1fr';
                  for (const img of images.slice(0, 4)) {
                    grid.appendChild(buildGridCell(img));
                  }
                }

                mountPhotoGrid(carousel, grid, images.length);
              }

              // X's native multi-image grid is a static CSS grid (not a swipe carousel),
              // so the snap-x/snap-mandatory detection above misses it. Without a rebuild,
              // a 3-photo post renders as a 2x2 grid with an empty 4th cell (the white box).
              // Mixed photo+video posts have the same parent grid; a photo-only rebuild
              // would drop the video. Walk remaining photo/video siblings and rebuild a
              // proper N-item grid (2/3/4). Skip public-API fallback grids — they are
              // already laid out, and a photo-only pass would delete their video cell.
              const nativeMediaParents = new Map();
              for (const media of root.querySelectorAll(
                '[data-testid="tweetPhoto"], [data-testid="videoPlayer"], [data-testid="videoComponent"]',
              )) {
                if (!(media instanceof HTMLElement)) continue;
                if (media.closest(`[${GRID_ATTR}]`)) continue;
                if (media.closest('[data-public-api-media-grid]')) continue;
                if (
                  media.getAttribute('data-testid') === 'videoComponent' &&
                  media.closest('[data-testid="videoPlayer"]')
                ) {
                  continue;
                }
                const parent = media.parentElement;
                if (!parent || parent === root) continue;
                if (parent.tagName === 'ARTICLE') continue;
                if (parent.hasAttribute('data-public-api-media-grid')) continue;
                if (!nativeMediaParents.has(parent)) nativeMediaParents.set(parent, []);
                nativeMediaParents.get(parent).push(media);
              }
              for (const [parent, mediaNodes] of nativeMediaParents) {
                if (mediaNodes.length < 2 || mediaNodes.length > 4) continue;
                const parentStyle = window.getComputedStyle(parent);
                if (parentStyle.display !== 'grid' && parentStyle.display !== 'inline-grid') continue;
                const entries = mediaNodes
                  .map((node) => {
                    const testid = node.getAttribute('data-testid');
                    if (testid === 'videoPlayer' || testid === 'videoComponent') {
                      return { type: 'video', node };
                    }
                    const img = node.querySelector('img');
                    return img && isMediaImage(img) ? { type: 'image', node: img } : null;
                  })
                  .filter(Boolean);
                if (entries.length < 2 || entries.length > 4) continue;

                const grid = buildMixedMediaGrid(entries);
                mountPhotoGrid(parent, grid, entries.length);
              }

              const tweetRoot = root.matches('article')
                ? root
                : root.querySelector('article') || root;
              const photoRoots = [...root.querySelectorAll('[data-testid="tweetPhoto"]')].filter(
                (node) => node instanceof HTMLElement,
              );
              const multiPhoto = photoRoots.length > 1;
              if (tweetRoot instanceof HTMLElement) {
                tweetRoot.toggleAttribute('data-resource-snapshot-single-photo', !multiPhoto && photoRoots.length === 1);
                tweetRoot.toggleAttribute('data-resource-snapshot-multi-photo', multiPhoto);
              }

              const constrainMediaTree = (mediaEl, { fillCover = false } = {}) => {
                if (!(mediaEl instanceof HTMLElement)) {
                  return;
                }
                mediaEl.style.setProperty('max-width', '100%', 'important');
                mediaEl.style.setProperty('width', '100%', 'important');
                mediaEl.style.setProperty('display', 'block', 'important');
                if (fillCover) {
                  // Multi-photo / rebuilt grids: keep center cover crop like X.
                  mediaEl.style.setProperty('height', '100%', 'important');
                  mediaEl.style.setProperty('object-fit', 'cover', 'important');
                  mediaEl.style.setProperty('object-position', 'center center', 'important');
                } else {
                  mediaEl.style.setProperty('height', 'auto', 'important');
                  mediaEl.style.setProperty('object-fit', 'contain', 'important');
                }

                let node = mediaEl.parentElement;
                while (node instanceof HTMLElement && node !== root) {
                  node.style.minWidth = '0';
                  node.style.maxWidth = '100%';
                  node.style.overflow = 'hidden';
                  if (node.getAttribute('data-testid') === 'tweetPhoto' ||
                      node.getAttribute('data-testid') === 'videoComponent' ||
                      node.getAttribute('data-testid') === 'videoPlayer') {
                    if (!multiPhoto || node.getAttribute('data-testid') !== 'tweetPhoto') {
                      node.style.width = '100%';
                    }
                    node.style.borderRadius = multiPhoto ? '0' : '16px';
                    break;
                  }
                  node = node.parentElement;
                }
              };

              for (const img of root.querySelectorAll('img')) {
                if (!isMediaImage(img)) {
                  continue;
                }
                // Custom rebuilt grids already use cover + height 100% via CSS.
                // Forcing height:auto here clips portrait cells to the top edge.
                if (img.closest(`[${GRID_ATTR}]`)) {
                  continue;
                }
                const inTweetPhoto = Boolean(img.closest('[data-testid="tweetPhoto"]'));
                if (multiPhoto && inTweetPhoto) {
                  constrainMediaTree(img, { fillCover: true });
                  continue;
                }
                if (inTweetPhoto && photoRoots.length === 1) {
                  constrainMediaTree(img, { fillCover: false });
                  continue;
                }
                // Guest / markup without tweetPhoto: keep the on-page frame.
                // Forcing height:auto blows a constrained portrait into full
                // natural height and leaves empty chrome under the tweet.
                img.style.setProperty('max-width', '100%', 'important');
              }

              for (const photoRoot of photoRoots) {
                photoRoot.style.overflow = 'hidden';
                photoRoot.style.maxWidth = '100%';
                photoRoot.style.minWidth = '0';
                if (!multiPhoto) {
                  photoRoot.style.borderRadius = '16px';
                  photoRoot.style.width = '100%';
                }
              }

              // Keep the outer multi-photo frame rounded like X.
              if (multiPhoto) {
                const firstPhoto = photoRoots[0];
                let frame = firstPhoto?.parentElement;
                while (frame instanceof HTMLElement && frame !== root) {
                  const photosInFrame = frame.querySelectorAll('[data-testid="tweetPhoto"]').length;
                  if (photosInFrame >= photoRoots.length) {
                    frame.style.borderRadius = '16px';
                    frame.style.overflow = 'hidden';
                    frame.style.maxWidth = '100%';
                    break;
                  }
                  frame = frame.parentElement;
                }
              }
            }
            """
        )
    except Exception:
        pass


def _prepare_tweet_for_screenshot(tweet_card) -> None:
    _prepare_tweet_media_for_screenshot(tweet_card)
    try:
        tweet_card.evaluate(
            """
            (root) => {
              const isVisible = (node) => {
                if (!(node instanceof Element)) {
                  return false;
                }
                const style = window.getComputedStyle(node);
                if (!style || style.display === 'none' || style.visibility === 'hidden') {
                  return false;
                }
                const rect = node.getBoundingClientRect();
                return rect.width >= 8 && rect.height >= 8;
              };

              const removeNode = (node) => {
                if (node instanceof Element) {
                  node.remove();
                }
              };

              const replyRestrictionPattern =
                /only some accounts can reply|who can reply|仅限部分|只有部分.*回复|回复受限|このポストに返信|返信できるの/i;

              // Guest detail pages put the lock banner in <output>; remove it and
              // any similarly worded chrome so it cannot pad the screenshot.
              root.querySelectorAll('output').forEach(removeNode);
              for (const node of [...root.querySelectorAll('div, aside, section, span')]) {
                if (!(node instanceof HTMLElement)) {
                  continue;
                }
                const text = (node.innerText || node.textContent || '').trim();
                if (!text || text.length > 120) {
                  continue;
                }
                if (replyRestrictionPattern.test(text)) {
                  const removable =
                    node.closest('output') ||
                    (node.children.length <= 3 ? node : null) ||
                    node;
                  removeNode(removable);
                }
              }

              const replySelectors = [
                '[data-testid="logged_out_read_replies_pivot"]',
                '[data-testid="inline_reply_offscreen"]',
              ];
              for (const selector of replySelectors) {
                root.querySelectorAll(selector).forEach(removeNode);
              }

              root.querySelectorAll('a, button, [role="button"], span').forEach((node) => {
                const text = (node.textContent || '').trim();
                if (!text || text.length > 80) {
                  return;
                }
                if (
                  /^(Read|See)\\s+\\d+[\\d,]*\\s+repl/i.test(text) ||
                  /^阅读\\s*\\d+/.test(text) ||
                  /^\\d+\\s*条回复/.test(text)
                ) {
                  removeNode(node);
                }
              });

              const tweetRoot = root.matches('article') ? root : root.querySelector('article') || root;
              tweetRoot.style.width = '100%';
              tweetRoot.style.maxWidth = '598px';
              tweetRoot.style.marginLeft = 'auto';
              tweetRoot.style.marginRight = 'auto';
              // Do not clip the article by a guessed engagement/footer height.
              // The timestamp/views and engagement row are part of the article.
              tweetRoot.style.setProperty('height', 'auto', 'important');
              tweetRoot.style.setProperty('max-height', 'none', 'important');
              tweetRoot.style.setProperty('overflow-y', 'visible', 'important');

              const engagementSelector = [
                '[data-testid="reply"]',
                '[data-testid="retweet"]',
                '[data-testid="like"]',
                '[data-testid="bookmark"]',
                '[data-testid="share"]',
              ].join(', ');
              const engagementAria =
                /^(reply|repost|retweet|like|bookmark|share|回复|转推|喜欢|收藏|分享)\\b/i;

              const findEngagementButtons = (scope) =>
                [...scope.querySelectorAll('button, a, [role="button"]')].filter((node) => {
                  if (!isVisible(node)) {
                    return false;
                  }
                  if (node.matches(engagementSelector) || node.closest(engagementSelector)) {
                    return true;
                  }
                  const label = (node.getAttribute('aria-label') || '').trim();
                  return engagementAria.test(label);
                });

              const rootRect = tweetRoot.getBoundingClientRect();
              const actionGroups = [...tweetRoot.querySelectorAll('[role="group"]')]
                .filter(isVisible)
                .map((node) => {
                  const rect = node.getBoundingClientRect();
                  return {
                    node,
                    rect,
                    text: (node.innerText || '').trim(),
                  };
                })
                .filter((entry) => {
                  if (entry.rect.width < 180 || entry.rect.height < 12) {
                    return false;
                  }
                  if (
                    entry.node.closest(
                      '[data-testid="videoComponent"], [data-testid="videoPlayer"]',
                    )
                  ) {
                    return false;
                  }
                  const hasEngagementButton = Boolean(
                    entry.node.querySelector(engagementSelector) ||
                      findEngagementButtons(entry.node).length > 0,
                  );
                  const looksLikeEngagement = /reply|repost|retweet|like|bookmark|share|回复|转推|喜欢|收藏|分享/i.test(
                    entry.text,
                  );
                  return hasEngagementButton || looksLikeEngagement;
                });
              let actionBar = actionGroups.sort(
                (a, b) => b.rect.bottom - a.rect.bottom,
              )[0]?.node;

              // Guest UI often has no role=group / data-testid; fall back to the
              // shared parent of aria-labelled engagement buttons.
              if (!actionBar) {
                const buttons = findEngagementButtons(tweetRoot);
                if (buttons.length >= 2) {
                  let common = buttons[0].parentElement;
                  while (
                    common &&
                    common !== tweetRoot &&
                    !buttons.every((button) => common.contains(button))
                  ) {
                    common = common.parentElement;
                  }
                  if (common && common !== tweetRoot) {
                    // Prefer a reasonably wide row rather than a tiny icon wrapper.
                    let candidate = common;
                    while (candidate && candidate !== tweetRoot) {
                      const rect = candidate.getBoundingClientRect();
                      if (rect.width >= Math.min(220, rootRect.width * 0.55)) {
                        actionBar = candidate;
                        break;
                      }
                      candidate = candidate.parentElement;
                    }
                    actionBar = actionBar || common;
                  }
                }
              }

              if (actionBar) {
                const isEngagementControl = (node) => {
                  if (!(node instanceof HTMLElement)) {
                    return false;
                  }

                  if (
                    node.matches(engagementSelector) ||
                    node.closest(engagementSelector)
                  ) {
                    return true;
                  }

                  const label = (node.getAttribute('aria-label') || '').trim();
                  return engagementAria.test(label);
                };

                const engagementCount = (node) => {
                  if (!(node instanceof HTMLElement)) {
                    return 0;
                  }
                  return [...node.querySelectorAll('button, a, [role="button"]')]
                    .filter(isEngagementControl).length;
                };

                const containsFooterMetadata = (node) => {
                  if (!(node instanceof HTMLElement)) {
                    return false;
                  }
                  const text = (node.innerText || '').trim();
                  return (
                    /(views|查看|次观看)/i.test(text) &&
                    /(\\d{1,2}:\\d{2}|年|AM|PM|·)/i.test(text)
                  );
                };

                // Only trim content after the bar when we are confident this is
                // the real engagement row. This avoids deleting detached count
                // wrappers used by newer X/Twitter layouts.
                if (engagementCount(actionBar) >= 3) {
                  const removeFollowing = (container, pivot) => {
                    let seen = false;
                    for (const child of [...container.children]) {
                      if (seen) {
                        if (containsFooterMetadata(child)) {
                          continue;
                        }

                        // Preserve any detached reply/repost/like/bookmark/share
                        // controls or count wrappers instead of removing them.
                        if (engagementCount(child) > 0) {
                          continue;
                        }

                        removeNode(child);
                        continue;
                      }
                      if (child === pivot || child.contains(pivot)) {
                        seen = true;
                      }
                    }
                  };

                  let pivot = actionBar;
                  while (pivot && pivot !== tweetRoot) {
                    const parent = pivot.parentElement;
                    if (!parent) {
                      break;
                    }
                    removeFollowing(parent, pivot);
                    if (parent === tweetRoot) {
                      break;
                    }
                    pivot = parent;
                  }
                }

              }

              // Final sweep in case the lock banner remounted or sat outside the
              // engagement-bar sibling walk.
              root.querySelectorAll('output').forEach(removeNode);
              for (const node of [...root.querySelectorAll('div, aside, section')]) {
                if (!(node instanceof HTMLElement)) {
                  continue;
                }
                const text = (node.innerText || '').trim();
                if (text && text.length <= 120 && replyRestrictionPattern.test(text)) {
                  removeNode(node);
                }
              }

              for (const node of root.querySelectorAll('div, aside, section')) {
                if (!(node instanceof HTMLElement)) {
                  continue;
                }
                const text = (node.innerText || '').trim();
                if (!text || text.length > 220) {
                  continue;
                }
                if (
                  /don't miss what's happening|sign up now to get your own|people on x are the first/i.test(
                    text,
                  )
                ) {
                  removeNode(node);
                }
              }

              const hideVideoControlChrome = (video) => {
                if (video.closest(`[${GRID_ATTR}]`)) {
                  video.controls = false;
                  return;
                }
                const findVideoPlayerRoot = (videoNode, cardRoot) => {
                  const explicit =
                    videoNode.closest('[data-testid="videoComponent"]') ||
                    videoNode.closest('[data-testid="videoPlayer"]');
                  if (explicit instanceof HTMLElement) {
                    return explicit;
                  }

                  const videoRect = videoNode.getBoundingClientRect();
                  let node = videoNode.parentElement;
                  while (node instanceof HTMLElement && node !== cardRoot) {
                    const rect = node.getBoundingClientRect();
                    if (
                      rect.width <= Math.max(videoRect.width * 1.8, videoRect.width + 96) &&
                      rect.height <= Math.max(videoRect.height * 2.5, videoRect.height + 96)
                    ) {
                      return node;
                    }
                    node = node.parentElement;
                  }

                  return videoNode.parentElement instanceof HTMLElement ? videoNode.parentElement : videoNode;
                };

                const playerRoot = findVideoPlayerRoot(video, tweetRoot);
                if (!(playerRoot instanceof HTMLElement) || playerRoot === tweetRoot) {
                  return;
                }
                if (/(views|查看|次观看)/i.test(playerRoot.innerText || '')) {
                  return;
                }

                video.controls = false;
                playerRoot.querySelectorAll(
                  'button,[role="button"],[role="progressbar"],[role="slider"],input[type="range"],svg,img',
                ).forEach((node) => {
                  if (!(node instanceof HTMLElement) || node === video || video.contains(node)) {
                    return;
                  }
                  node.style.setProperty('display', 'none', 'important');
                });

                const rootRect = playerRoot.getBoundingClientRect();
                for (const node of playerRoot.querySelectorAll('div')) {
                  if (!(node instanceof HTMLElement) || node === video || video.contains(node) || node.contains(video)) {
                    continue;
                  }
                  const rect = node.getBoundingClientRect();
                  if (rect.width < rootRect.width * 0.45 || rect.height < 4 || rect.height > 96) {
                    continue;
                  }
                  if (rect.bottom >= rootRect.bottom - 8 && rect.top >= rootRect.bottom - 100) {
                    node.style.setProperty('display', 'none', 'important');
                    node.style.setProperty('background', 'transparent', 'important');
                  }
                }

                const videoRect = video.getBoundingClientRect();
                if (videoRect.height > 0) {
                  const topOffset = Math.max(0, videoRect.top - rootRect.top);
                  const targetHeight = Math.ceil(topOffset + videoRect.height);
                  playerRoot.style.height = `${targetHeight}px`;
                  playerRoot.style.maxHeight = `${targetHeight}px`;
                  playerRoot.style.overflow = 'hidden';
                  playerRoot.style.background = 'transparent';
                  playerRoot.style.paddingBottom = '0';
                }
              };

              const trimVideoContainer = (video) => {
                if (video.closest(`[${GRID_ATTR}]`)) {
                  video.style.width = '100%';
                  video.style.height = '100%';
                  video.style.objectFit = 'cover';
                  video.style.background = 'transparent';
                  return;
                }
                const rect = video.getBoundingClientRect();
                if (rect.width < 1 || rect.height < 1) {
                  return;
                }

                let sourceWidth = video.videoWidth;
                let sourceHeight = video.videoHeight;
                if (!sourceWidth || !sourceHeight) {
                  sourceWidth = rect.width;
                  sourceHeight = rect.height;
                }
                if (!sourceWidth || !sourceHeight) {
                  return;
                }

                const aspect = sourceWidth / sourceHeight;
                const targetWidth = Math.min(rect.height * aspect, rect.width);
                const playerRoot =
                  video.closest('[data-testid="videoComponent"]') ||
                  video.closest('[data-testid="videoPlayer"]') ||
                  video.parentElement;
                if (!(playerRoot instanceof HTMLElement)) {
                  return;
                }

                const chain = [];
                let current = video;
                while (current && current !== playerRoot) {
                  chain.push(current);
                  current = current.parentElement;
                }
                chain.push(playerRoot);

                for (const node of chain) {
                  if (!(node instanceof HTMLElement)) {
                    continue;
                  }
                  node.style.width = `${targetWidth}px`;
                  node.style.maxWidth = `${targetWidth}px`;
                  node.style.minWidth = '0';
                  node.style.marginLeft = 'auto';
                  node.style.marginRight = 'auto';
                  node.style.background = 'transparent';
                  node.style.overflow = 'hidden';
                }

                video.style.width = '100%';
                video.style.height = 'auto';
                video.style.objectFit = 'contain';
                video.style.background = 'transparent';
              };

              const stabilizeFooterLayout = () => {
                for (const row of tweetRoot.querySelectorAll('[role="group"]')) {
                  if (!(row instanceof HTMLElement)) {
                    continue;
                  }
                  const rect = row.getBoundingClientRect();
                  if (rect.width < 80 || rect.height > 96) {
                    continue;
                  }
                  row.style.width = '100%';
                  row.style.maxWidth = '100%';
                  row.style.minWidth = '0';
                  row.style.flex = '1 1 auto';
                }

                for (const row of tweetRoot.querySelectorAll('div')) {
                  if (!(row instanceof HTMLElement)) {
                    continue;
                  }
                  const text = (row.innerText || '').trim();
                  if (!/(views|查看|次观看)/i.test(text)) {
                    continue;
                  }
                  if (!/(\\d{1,2}:\\d{2}|年|AM|PM|·)/i.test(text)) {
                    continue;
                  }
                  const rect = row.getBoundingClientRect();
                  if (rect.height > 60 || rect.width < 120) {
                    continue;
                  }
                  row.style.width = '100%';
                  row.style.maxWidth = '100%';

                  for (const span of row.querySelectorAll('span')) {
                    if (!(span instanceof HTMLElement)) {
                      continue;
                    }
                    const label = (span.textContent || '').trim();
                    if (/^(views|查看|次观看)$/i.test(label)) {
                      span.style.display = 'inline';
                      span.style.marginLeft = '0.25em';
                    }
                    if (/^[\\d,.]+万?$/.test(label)) {
                      span.style.display = 'inline';
                    }
                  }
                }
              };

              for (const video of root.querySelectorAll('video')) {
                if (!isVisible(video)) {
                  continue;
                }
                hideVideoControlChrome(video);
                trimVideoContainer(video);
              }

              stabilizeFooterLayout();
            }
            """
        )
    except Exception:
        pass


def _compute_capture_clip(page, tweet_card):
    element = tweet_card.element_handle(timeout=5000)
    if element is None:
        return None

    clip_script = """
        (el) => {
          const doc = document.documentElement;
          const rootRect = el.getBoundingClientRect();
          const mediaSelector = 'img, svg, video, canvas, picture, iframe';
          const excludedSelector = [
            '[data-testid="logged_out_read_replies_pivot"]',
            '[data-testid="inline_reply_offscreen"]',
            '[data-testid="reply"]',
            '[data-testid="tweetTextarea_0"]',
            '[data-testid="inline_reply_composer"]',
            '[contenteditable="true"][role="textbox"]',
            '[role="textbox"]',
            'form[aria-label*="Reply"]',
            'form[aria-label*="reply"]',
            'form[aria-label*="回复"]',
            'form[aria-label*="回覆"]',
            'output',
          ].join(', ');

          let left = Infinity;
          let top = Infinity;
          let right = -Infinity;
          let bottom = -Infinity;

          const addRect = (rect) => {
            if (!rect || rect.width < 2 || rect.height < 2) {
              return;
            }
            left = Math.min(left, rect.left + window.scrollX);
            top = Math.min(top, rect.top + window.scrollY);
            right = Math.max(right, rect.right + window.scrollX);
            bottom = Math.max(bottom, rect.bottom + window.scrollY);
          };

          const isVisible = (node) => {
            const style = window.getComputedStyle(node);
            if (!style) {
              return false;
            }
            if (style.display === 'none' || style.visibility === 'hidden') {
              return false;
            }
            if (Number(style.opacity || '1') === 0) {
              return false;
            }
            return true;
          };

          const shouldExclude = (node) => {
            if (!node || !(node instanceof Element)) {
              return false;
            }
            return Boolean(node.closest(excludedSelector));
          };

          const textRects = () => {
            const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
            while (walker.nextNode()) {
              const textNode = walker.currentNode;
              if (!textNode.textContent || !textNode.textContent.trim()) {
                continue;
              }
              const parent = textNode.parentElement;
              if (!parent || !isVisible(parent)) {
                continue;
              }
              if (shouldExclude(parent)) {
                continue;
              }

              const range = document.createRange();
              range.selectNodeContents(textNode);
              for (const rect of range.getClientRects()) {
                addRect(rect);
              }
            }
          };

          textRects();

          const headerSelector = [
            '[data-testid="User-Name"]',
            '[data-testid="Tweet-User-Avatar"]',
            '[data-testid="UserAvatar-Container-Unknown"]',
          ].join(', ');
          for (const node of el.querySelectorAll(headerSelector)) {
            if (!isVisible(node)) {
              continue;
            }
            addRect(node.getBoundingClientRect());
          }

          const isTweetBodyText = (node) => {
            if (!(node instanceof Element)) {
              return false;
            }
            if (node.closest('[data-testid="User-Name"]')) {
              return false;
            }
            if (node.closest('[data-testid="socialContext"]')) {
              return false;
            }
            if (node.closest('[role="group"]')) {
              return false;
            }
            return true;
          };

          for (const selector of ['[data-testid="tweetText"]', 'div[dir="auto"]']) {
            for (const node of el.querySelectorAll(selector)) {
              if (!isVisible(node) || shouldExclude(node) || !isTweetBodyText(node)) {
                continue;
              }
              addRect(node.getBoundingClientRect());
            }
          }

          for (const node of el.querySelectorAll('[__TRANSLATION_ATTR__="block"]')) {
            if (!isVisible(node)) {
              continue;
            }
            addRect(node.getBoundingClientRect());
          }

          for (const node of el.querySelectorAll(mediaSelector)) {
            if (!isVisible(node)) {
              continue;
            }
            if (shouldExclude(node)) {
              continue;
            }
            if (
              node instanceof HTMLImageElement &&
              node.closest('[data-testid="Tweet-User-Avatar"], [data-testid="UserAvatar-Container-Unknown"], [data-testid="User-Name"]')
            ) {
              continue;
            }
            for (const rect of node.getClientRects()) {
              addRect(rect);
            }
          }

          const engagementSelector = [
            '[data-testid="reply"]',
            '[data-testid="retweet"]',
            '[data-testid="like"]',
            '[data-testid="bookmark"]',
            '[data-testid="share"]',
          ].join(', ');
          const engagementAria =
            /^(reply|repost|retweet|like|bookmark|share|回复|转推|喜欢|收藏|分享)\\b/i;

          const findEngagementButtons = (scope) =>
            [...scope.querySelectorAll('button, a, [role="button"]')].filter((node) => {
              if (!isVisible(node)) {
                return false;
              }
              if (node.matches(engagementSelector) || node.querySelector(engagementSelector)) {
                return true;
              }
              // Guest UI uses aria-label without data-testid.
              if (node.closest(engagementSelector)) {
                return true;
              }
              const label = (node.getAttribute('aria-label') || '').trim();
              return engagementAria.test(label);
            });

          const actionGroups = [...el.querySelectorAll('[role="group"]')]
            .filter((node) => isVisible(node))
            .map((node) => {
              const rect = node.getBoundingClientRect();
              return {
                node,
                top: rect.top + window.scrollY,
                bottom: rect.bottom + window.scrollY,
                width: rect.width,
                height: rect.height,
                text: (node.innerText || '').trim(),
                hasEngagementButton: Boolean(
                  node.querySelector(engagementSelector) ||
                    findEngagementButtons(node).length > 0,
                ),
              };
            })
            .filter((rect) => {
              if (rect.width < 180 || rect.height < 12) {
                return false;
              }
              const nearBottom = rect.bottom >= rootRect.top + rootRect.height * 0.45;
              const looksLikeEngagement = /reply|repost|retweet|like|bookmark|share|回复|转推|喜欢|收藏|分享/i.test(
                rect.text,
              );
              return rect.hasEngagementButton || nearBottom || looksLikeEngagement;
            });

          let actionBar = actionGroups.sort((a, b) => {
            if (a.hasEngagementButton !== b.hasEngagementButton) {
              return a.hasEngagementButton ? -1 : 1;
            }
            return b.bottom - a.bottom;
          })[0];

          if (!actionBar) {
            const buttons = findEngagementButtons(el);
            if (buttons.length > 0) {
              let bottom = -Infinity;
              let top = Infinity;
              let leftmost = Infinity;
              let rightmost = -Infinity;
              for (const button of buttons) {
                const rect = button.getBoundingClientRect();
                bottom = Math.max(bottom, rect.bottom + window.scrollY);
                top = Math.min(top, rect.top + window.scrollY);
                leftmost = Math.min(leftmost, rect.left + window.scrollX);
                rightmost = Math.max(rightmost, rect.right + window.scrollX);
              }
              if (Number.isFinite(bottom)) {
                actionBar = {
                  top,
                  bottom,
                  width: Math.max(1, rightmost - leftmost),
                  height: Math.max(1, bottom - top),
                  text: '',
                  hasEngagementButton: true,
                };
              }
            }
          }

          const rootX = rootRect.left + window.scrollX;
          const rootY = rootRect.top + window.scrollY;
          const rootRight = rootX + rootRect.width;
          const rootBottom = rootY + rootRect.height;

          if (!Number.isFinite(left)) {
            left = rootX;
            top = rootY;
            right = rootRight;
            bottom = rootBottom;
          } else {
            top = Math.min(top, rootY);
            // Prefer measured content over the full article box. Conversation
            // chrome / trailing spacers can inflate rootBottom past the tweet.
            if (!actionBar) {
              bottom = Math.max(bottom, rootBottom);
            }
          }

          left = rootX;
          right = rootRight;

          const padding = 12;
          const bottomPadding = actionBar ? 8 : padding;
          const x = Math.max(0, Math.floor(left - padding));
          const y = Math.max(0, Math.floor(top - padding));
          const maxRight = Math.max(doc.scrollWidth, right + padding);
          // Hard-stop at the engagement row so reply-lock banners / replies
          // under the detail tweet never enter the capture rectangle.
          const contentBottom = actionBar
            ? actionBar.bottom
            : Math.min(bottom, rootBottom);
          const maxBottom = Math.max(doc.scrollHeight, contentBottom + bottomPadding);
          const width = Math.max(1, Math.ceil(Math.min(maxRight, right + padding) - x));
          let height = Math.max(1, Math.ceil(Math.min(maxBottom, contentBottom + bottomPadding) - y));

          // Guard against flex/min-content blowups from large orig/srcset images.
          // A detail card with quote media should rarely exceed ~4x column width.
          const maxHeight = Math.max(Math.ceil(rootRect.width * 4.5), 2400);
          if (height > maxHeight) {
            height = maxHeight;
          }

          return { x, y, width, height };
        }
        """.replace("__TRANSLATION_ATTR__", TRANSLATION_ATTR)

    return page.evaluate(
        clip_script,
        arg=element,
    )


def _ensure_viewport_can_fit_clip(page, clip: dict[str, int] | None) -> bool:
    if not clip:
        return False

    viewport = page.viewport_size or {
        "width": DEFAULT_VIEWPORT_WIDTH,
        "height": DEFAULT_VIEWPORT_HEIGHT,
    }
    required_width = max(int(clip["width"]) + CAPTURE_VIEWPORT_MARGIN, min(viewport["width"], 760))
    required_height = max(int(clip["height"]) + CAPTURE_VIEWPORT_MARGIN, DEFAULT_VIEWPORT_HEIGHT)

    if required_width <= viewport["width"] and required_height <= viewport["height"]:
        return False

    page.set_viewport_size(
        {
            "width": required_width,
            "height": required_height,
        }
    )
    page.wait_for_timeout(300)
    return True


def _capture_detail_snapshot(
    page,
    tweet_card,
    path: Path,
    *,
    tweet_id: str | None = None,
    public_api_fallback: bool = False,
    guest_mode: bool = False,
) -> None:
    """Capture the target tweet article itself.

    X's current detail DOM keeps timestamp/views and the reply/repost/like/bookmark
    row inside <article>, while the reply composer is a sibling outside <article>.
    Taking an element screenshot is therefore safer than guessing a clip bottom.
    """
    _hide_non_primary_columns(page, tweet_id)
    _scroll_tweet_into_view(page, tweet_card, guest_mode=guest_mode)
    _hide_non_primary_columns(page, tweet_id)

    try:
        tweet_card.evaluate(
            """
            (root) => {
              const article = root.matches('article')
                ? root
                : root.querySelector('article') || root;

              if (article instanceof HTMLElement) {
                article.style.setProperty('height', 'auto', 'important');
                article.style.setProperty('max-height', 'none', 'important');
                article.style.setProperty('overflow-y', 'visible', 'important');
                article.style.setProperty('padding-bottom', '18px', 'important');
                article.style.setProperty('box-sizing', 'border-box', 'important');
              }

              // X currently renders the engagement metrics inside a role=group
              // at the very bottom of the article. Give that row breathing room
              // so element screenshots do not shave off icon/number descenders.
              const engagementGroup = [...article.querySelectorAll('[role="group"]')]
                .find((node) =>
                  node.querySelector(
                    '[data-testid="reply"], [data-testid="retweet"], [data-testid="like"], [data-testid="bookmark"], [data-testid="removeBookmark"]'
                  )
                );
              if (engagementGroup instanceof HTMLElement) {
                engagementGroup.style.setProperty('margin-bottom', '10px', 'important');
                engagementGroup.style.setProperty('padding-left', '12px', 'important');
                engagementGroup.style.setProperty('padding-right', '12px', 'important');
                engagementGroup.style.setProperty('box-sizing', 'border-box', 'important');
                engagementGroup.style.setProperty('overflow', 'visible', 'important');
              }

              // Photo-only fallback grids: align heights per row instead of
              // X's fixed mosaic (which crops tall screenshots and text) or
              // fully auto heights (which look ragged when photos come from
              // different devices). Each row takes the tallest image's
              // natural height at the column width: taller photos stay
              // intact, shorter ones are cover-cropped (losing left/right
              // edges) to match the row.
              for (const grid of article.querySelectorAll('.media-grid')) {
                const cells = [...grid.children];
                if (!cells.length || !cells.every((cell) =>
                  cell.matches('[data-testid="tweetPhoto"]') && cell.querySelector('img'))) {
                  continue;
                }
                const imgs = cells.map((cell) => cell.querySelector('img'));
                if (!imgs.every((img) => img.complete && img.naturalWidth > 0)) {
                  // Sizes unknown yet: fall back to X-style fixed mosaic.
                  continue;
                }
                if (cells.length === 1) {
                  // Single photo: keep the complete image, unconstrained.
                  grid.style.setProperty('aspect-ratio', 'auto', 'important');
                  grid.style.setProperty('grid-template-rows', 'none', 'important');
                  grid.style.setProperty('align-items', 'start', 'important');
                  const cell = cells[0];
                  cell.style.setProperty('height', 'auto', 'important');
                  cell.style.setProperty('min-height', '0', 'important');
                  cell.style.setProperty('max-height', 'none', 'important');
                  const img = imgs[0];
                  img.style.setProperty('width', '100%', 'important');
                  img.style.setProperty('height', 'auto', 'important');
                  img.style.setProperty('max-height', 'none', 'important');
                  img.style.setProperty('object-fit', 'contain', 'important');
                  continue;
                }

                const gap = 2;
                const gridWidth = grid.getBoundingClientRect().width;
                const columnWidth = Math.max((gridWidth - gap) / 2, 1);
                const naturalHeights = imgs.map((img) =>
                  columnWidth * img.naturalHeight / img.naturalWidth);

                let rowHeights;
                if (grid.classList.contains('n3')) {
                  // Left photo spans both rows; its natural height splits
                  // across the two rows. Each row takes the taller of the
                  // right-column photo and the left photo's per-row share;
                  // the shorter one is cover-cropped.
                  const rowShare = Math.max((naturalHeights[0] - gap) / 2, 1);
                  rowHeights = [
                    Math.max(naturalHeights[1], rowShare),
                    Math.max(naturalHeights[2], rowShare),
                  ];
                } else if (cells.length >= 4) {
                  rowHeights = [
                    Math.max(naturalHeights[0], naturalHeights[1]),
                    Math.max(naturalHeights[2], naturalHeights[3]),
                  ];
                } else {
                  rowHeights = [Math.max(naturalHeights[0], naturalHeights[1])];
                }

                // Cells are already height:100% (stretch) via CSS; only the
                // grid rows and the img 720px cap need overriding.
                grid.style.setProperty('aspect-ratio', 'auto', 'important');
                grid.style.setProperty(
                  'grid-template-rows',
                  rowHeights.map((h) => `${Math.round(h)}px`).join(' '),
                  'important'
                );
                grid.style.setProperty('align-items', 'stretch', 'important');
                for (const img of imgs) {
                  img.style.setProperty('width', '100%', 'important');
                  img.style.setProperty('height', '100%', 'important');
                  img.style.setProperty('max-height', 'none', 'important');
                  img.style.setProperty('object-fit', 'cover', 'important');
                  img.style.setProperty('object-position', 'center center', 'important');
                }
              }

              // Freeze each visible X video into a canvas after the requested
              // frame has already been selected. This removes every player
              // overlay (including ordinary div-based progress bars) without
              // changing the captured frame.
              for (const video of [...article.querySelectorAll('video')]) {
                if (!(video instanceof HTMLVideoElement)) {
                  continue;
                }
                const rect = video.getBoundingClientRect();
                if (rect.width < 2 || rect.height < 2 || video.videoWidth < 2 || video.videoHeight < 2) {
                  continue;
                }

                const player =
                  video.closest('[data-testid="videoComponent"]') ||
                  video.closest('[data-testid="videoPlayer"]') ||
                  video.parentElement;
                if (!(player instanceof HTMLElement)) {
                  continue;
                }

                try {
                  const canvas = document.createElement('canvas');
                  canvas.width = video.videoWidth;
                  canvas.height = video.videoHeight;
                  const ctx = canvas.getContext('2d');
                  if (!ctx) {
                    continue;
                  }
                  ctx.drawImage(video, 0, 0, canvas.width, canvas.height);

                  // Bound tall single-video cards by shrinking the whole frame,
                  // rather than clipping the video or adding letterbox space.
                  const singleMediaGrid = player.closest('.media-grid.n1');
                  if (singleMediaGrid instanceof HTMLElement) {
                    const maxFrameHeight = 600;
                    const maxFrameWidth = maxFrameHeight * canvas.width / canvas.height;
                    if (article.querySelectorAll('.media-grid').length === 1 &&
                        article.querySelectorAll('video').length === 1 &&
                        canvas.height > canvas.width) {
                      const cardWidth = article.getBoundingClientRect().width;
                      const inset = Math.max(0, cardWidth - singleMediaGrid.getBoundingClientRect().width);
                      // Reflow text and metrics with the portrait frame instead
                      // of keeping a wide card around a narrow centered video.
                      const compactWidth = Math.min(cardWidth, Math.max(390, maxFrameWidth + inset));
                      article.style.setProperty('width', `${compactWidth}px`, 'important');
                      article.style.setProperty('max-width', `${compactWidth}px`, 'important');
                      article.style.setProperty('min-width', '0', 'important');
                      article.setAttribute('data-resource-snapshot-compact', 'true');
                    }
                    const mediaMaxWidth = article.hasAttribute('data-resource-snapshot-compact')
                      ? '100%' : `${maxFrameWidth}px`;
                    singleMediaGrid.style.setProperty('max-width', mediaMaxWidth, 'important');
                    singleMediaGrid.style.setProperty('margin-left', 'auto', 'important');
                    singleMediaGrid.style.setProperty('margin-right', 'auto', 'important');
                    // Earlier player cleanup may have pinned its old pixel
                    // height. Recompute it after the narrower grid has reflowed.
                    const frameWidth = player.getBoundingClientRect().width;
                    player.style.setProperty('height', `${frameWidth * canvas.height / canvas.width}px`, 'important');
                    player.style.setProperty('min-height', '0', 'important');
                    player.style.setProperty('max-height', 'none', 'important');
                  }

                  canvas.setAttribute('data-resource-snapshot-video-frame', 'true');
                  canvas.style.position = 'absolute';
                  canvas.style.inset = '0';
                  canvas.style.width = '100%';
                  canvas.style.height = '100%';
                  canvas.style.objectFit = 'contain';
                  canvas.style.objectPosition = 'center center';
                  canvas.style.display = 'block';
                  canvas.style.zIndex = '999';
                  canvas.style.pointerEvents = 'none';
                  canvas.style.background = '#000';

                  player.style.position = 'relative';
                  player.style.overflow = 'hidden';
                  player.appendChild(canvas);

                  // Hide the actual media element and every overlay under the
                  // canvas. The canvas remains the only visible player layer.
                  video.style.setProperty('visibility', 'hidden', 'important');
                  for (const child of [...player.children]) {
                    if (child === canvas) {
                      continue;
                    }
                    if (child instanceof HTMLElement) {
                      child.style.setProperty('visibility', 'hidden', 'important');
                    }
                  }
                  canvas.style.setProperty('visibility', 'visible', 'important');
                } catch (error) {
                  // If drawing fails, fall back to aggressively hiding known UI.
                  player.querySelectorAll(
                    'button,[role="button"],[role="progressbar"],[role="slider"],input[type="range"]'
                  ).forEach((node) => {
                    if (node instanceof HTMLElement) {
                      node.style.setProperty('display', 'none', 'important');
                    }
                  });
                }
              }

              // Remove controls below the engagement row that are still inside
              // the article ("Related", "View quotes"), but keep timestamp/views
              // and all engagement metrics.
              const junkPatterns = [
                /^(相关|Related)$/i,
                /^(查看引用|View quotes|See quotes)$/i,
              ];

              for (const node of [...article.querySelectorAll('button, a')]) {
                if (!(node instanceof HTMLElement)) {
                  continue;
                }
                const label = (node.innerText || node.textContent || '').trim();
                if (!junkPatterns.some((pattern) => pattern.test(label))) {
                  continue;
                }

                // These two controls normally share one bottom row.
                let row = node.parentElement;
                while (
                  row instanceof HTMLElement &&
                  row.parentElement instanceof HTMLElement &&
                  row !== article
                ) {
                  const parentText = (row.parentElement.innerText || '').trim();
                  if (
                    parentText.length <= 80 &&
                    (/相关|Related/i.test(parentText)) &&
                    (/查看引用|View quotes|See quotes/i.test(parentText))
                  ) {
                    row = row.parentElement;
                    break;
                  }
                  row = row.parentElement;
                }

                if (row instanceof HTMLElement && row !== article) {
                  row.remove();
                } else {
                  node.remove();
                }
              }
            }
            """
        )
    except Exception:
        pass

    page.wait_for_timeout(150)

    # Capture slightly outside the article itself. Element screenshots use the
    # element's exact border box, which can visually shave rounded borders and
    # edge controls. A small page-level margin preserves the complete outer card.
    box = tweet_card.bounding_box()
    if not box:
        tweet_card.screenshot(
            path=str(path),
            animations="disabled",
        )
        return

    outer_margin = 10
    viewport = page.viewport_size or {
        "width": DEFAULT_VIEWPORT_WIDTH,
        "height": DEFAULT_VIEWPORT_HEIGHT,
    }

    x = max(0, int(box["x"]) - outer_margin)
    y = max(0, int(box["y"]) - outer_margin)
    right = min(
        int(viewport["width"]),
        int(box["x"] + box["width"]) + outer_margin,
    )
    bottom = int(box["y"] + box["height"]) + outer_margin

    required_height = bottom + CAPTURE_VIEWPORT_MARGIN
    if required_height > int(viewport["height"]):
        page.set_viewport_size(
            {
                "width": int(viewport["width"]),
                "height": required_height,
            }
        )
        page.wait_for_timeout(100)

        # Re-read the box because viewport resize can reflow X.
        box = tweet_card.bounding_box()
        if box:
            x = max(0, int(box["x"]) - outer_margin)
            y = max(0, int(box["y"]) - outer_margin)
            right = min(
                int((page.viewport_size or viewport)["width"]),
                int(box["x"] + box["width"]) + outer_margin,
            )
            bottom = int(box["y"] + box["height"]) + outer_margin

    page.screenshot(
        path=str(path),
        animations="disabled",
        clip={
            "x": x,
            "y": y,
            "width": max(1, right - x),
            "height": max(1, bottom - y),
        },
    )
