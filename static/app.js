const translateBtn = document.getElementById('translateBtn');
    const captureBtn = document.getElementById('captureBtn');
    const input = document.getElementById('url');
    const videoTimes = document.getElementById('videoTimes');
    const showBrowser = document.getElementById('showBrowser');
    const darkMode = document.getElementById('darkMode');
    const anonymous = document.getElementById('anonymous');
    const translationReview = document.getElementById('translationReview');
    const reviewHint = document.getElementById('reviewHint');
    const translationList = document.getElementById('translationList');
    const status = document.getElementById('status');
    const resultCard = document.getElementById('resultCard');
    const emptyState = document.getElementById('emptyState');
    const previewImage = document.getElementById('previewImage');
    const fileName = document.getElementById('fileName');
    const savedTo = document.getElementById('savedTo');
    const captureMode = document.getElementById('captureMode');
    const videoFrameRow = document.getElementById('videoFrameRow');
    const videoFrame = document.getElementById('videoFrame');
    const usedUrl = document.getElementById('usedUrl');
    const openImage = document.getElementById('openImage');
    const saveHint = document.getElementById('saveHint');
    let translationDraft = [];
    let translationPrepared = false;
    let translationRevision = 0;
    let translationTweetId = null;
    let busy = false;

    saveHint.textContent = '当前工具文件夹里的 screenshots/ 子目录';

    function setStatus(message, kind = '') {
      status.textContent = message || '';
      status.className = 'status' + (kind ? ` ${kind}` : '');
    }

    function formatVideoTime(totalSeconds) {
      const numericValue = Number(totalSeconds);
      if (!Number.isFinite(numericValue) || numericValue < 0) {
        return '';
      }

      const rounded = Math.round(numericValue * 10) / 10;
      const hours = Math.floor(rounded / 3600);
      const minutes = Math.floor((rounded % 3600) / 60);
      const seconds = rounded - hours * 3600 - minutes * 60;
      const hasFraction = Math.abs(seconds - Math.round(seconds)) > 0.001;
      const secondsText = seconds.toFixed(hasFraction ? 1 : 0).padStart(hasFraction ? 4 : 2, '0');

      if (hours > 0) {
        return `${hours}:${String(minutes).padStart(2, '0')}:${secondsText}`;
      }
      return `${String(minutes).padStart(2, '0')}:${secondsText}`;
    }

    function getBasePayload() {
      return {
        url: input.value.trim(),
        videoTimes: videoTimes.value.trim(),
        showBrowser: showBrowser.checked,
        anonymous: anonymous.checked,
        darkMode: darkMode.checked
      };
    }

    function setBusy(isBusy) {
      busy = isBusy;
      translateBtn.disabled = isBusy;
      captureBtn.disabled = isBusy;
    }

    function tweetIdFromUrl(value) {
      try {
        const url = new URL(/^https?:\/\//i.test(value) ? value : 'https://' + value);
        if (!['x.com', 'www.x.com', 'twitter.com', 'www.twitter.com', 'mobile.twitter.com'].includes(url.hostname)) return null;
        return url.pathname.match(/^\/[^/]+\/status\/(\d+)(?:\/|$)/)?.[1] || null;
      } catch {
        return null;
      }
    }

    function resetTranslationReview() {
      translationRevision += 1;
      translationTweetId = null;
      translationDraft = [];
      translationPrepared = false;
      translationList.innerHTML = '';
      reviewHint.textContent = '点击“获取翻译”后，这里会显示逐段原文和可编辑中文。随后点击“开始截图”会把这里的内容带进最终图片。';
      translationReview.classList.remove('show');
      translateBtn.textContent = '获取翻译';
    }

    async function copyText(text, button) {
      const value = text || '';
      if (!value) {
        return;
      }

      const originalLabel = button.textContent;
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          await navigator.clipboard.writeText(value);
        } else {
          const helper = document.createElement('textarea');
          helper.value = value;
          helper.setAttribute('readonly', 'readonly');
          helper.style.position = 'fixed';
          helper.style.opacity = '0';
          document.body.appendChild(helper);
          helper.select();
          document.execCommand('copy');
          document.body.removeChild(helper);
        }

        button.textContent = '已复制';
        button.classList.add('copied');
        window.setTimeout(() => {
          button.textContent = originalLabel;
          button.classList.remove('copied');
        }, 1400);
      } catch (error) {
        setStatus('复制原文失败，请手动选中复制', 'error');
      }
    }

    function renderTranslationReview() {
      translationList.innerHTML = '';
      translationReview.classList.add('show');

      if (!translationDraft.length) {
        reviewHint.textContent = '没有检测到需要翻译的正文。现在直接点击“开始截图”即可生成原图。';
        return;
      }

      reviewHint.textContent = '下面会按正文出现顺序列出原文和对应中文。你可以复制原文去第三方翻译，也可以直接修改右侧中文；然后点击“开始截图”生成最终图片。';

      translationDraft.forEach((item, position) => {
        const card = document.createElement('div');
        card.className = 'review-item';

        const head = document.createElement('div');
        head.className = 'review-item-head';

        const title = document.createElement('h3');
        title.textContent = item.label || `第 ${position + 1} 段正文`;

        head.append(title);

        const contentGrid = document.createElement('div');
        contentGrid.className = 'review-grid';

        const sourceBlock = document.createElement('div');
        sourceBlock.className = 'review-block';

        const sourceHead = document.createElement('div');
        sourceHead.className = 'review-block-head';

        const sourceLabel = document.createElement('span');
        sourceLabel.className = 'review-caption';
        sourceLabel.textContent = '原文';

        const copyBtn = document.createElement('button');
        copyBtn.type = 'button';
        copyBtn.className = 'button-secondary review-copy';
        copyBtn.textContent = '复制原文';
        copyBtn.addEventListener('click', async () => {
          await copyText(item.originalText, copyBtn);
        });

        sourceHead.append(sourceLabel, copyBtn);

        const source = document.createElement('div');
        source.className = 'review-source';
        source.textContent = item.originalText;

        sourceBlock.append(sourceHead, source);

        const translationBlock = document.createElement('div');
        translationBlock.className = 'review-block';

        const translationHead = document.createElement('div');
        translationHead.className = 'review-block-head';

        const translationLabel = document.createElement('span');
        translationLabel.className = 'review-caption';
        translationLabel.textContent = '中文翻译';

        translationHead.append(translationLabel);

        const textarea = document.createElement('textarea');
        textarea.className = 'review-translation';
        textarea.value = item.translation || '';
        textarea.placeholder = '这里可以直接修改这一段的中文翻译';
        textarea.addEventListener('input', () => {
          item.translation = textarea.value;
        });

        translationBlock.append(translationHead, textarea);
        contentGrid.append(sourceBlock, translationBlock);
        card.append(head, contentGrid);
        translationList.append(card);
      });

      translationReview.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    function updateResult(data) {
      const previewUrl = `${data.previewUrl}?t=${Date.now()}`;
      previewImage.src = previewUrl;
      previewImage.alt = `推文截图 ${data.fileName}`;
      fileName.textContent = data.fileName;
      savedTo.textContent = data.savedTo;
      const modeLabels = {
        detail_page: '详情页截图',
        public_api_fallback: '公开数据重绘（非原网页截图）'
      };
      captureMode.textContent = modeLabels[data.captureMode] || '其他截图来源';
      const frames = Array.isArray(data.videoFrames) ? data.videoFrames : [];
      if (frames.length > 0) {
        videoFrame.innerHTML = frames
          .map((frame) => {
            const label = frame.label || `视频 ${Number(frame.index) + 1}`;
            const time = formatVideoTime(frame.seconds);
            return `<span class="video-frame-chip"><strong>${label}</strong> <code>${time}</code></span>`;
          })
          .join('');
        videoFrameRow.hidden = false;
      } else if (typeof data.videoFrameSeconds === 'number') {
        videoFrame.innerHTML = `<code>${formatVideoTime(data.videoFrameSeconds)}</code>`;
        videoFrameRow.hidden = false;
      } else {
        videoFrame.textContent = '';
        videoFrameRow.hidden = true;
      }
      const sourceUrl = data.captureMode === 'public_api_fallback'
        ? `https://x.com/i/status/${data.tweetId}`
        : data.usedUrl;
      usedUrl.href = sourceUrl;
      usedUrl.textContent = sourceUrl;
      openImage.href = previewUrl;

      emptyState.style.display = 'none';
      resultCard.classList.add('show');
    }

    async function requestCapture(payload) {
      const response = await fetch('/api/capture', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });

      const data = await response.json().catch(() => ({}));
      if (!response.ok || !data.ok) {
        throw new Error(data.error || '截图失败');
      }
      return data;
    }

    async function prepareTranslations() {
      if (busy) return;
      const url = input.value.trim();
      if (!url) {
        setStatus('请输入推文链接', 'error');
        return;
      }

      setBusy(true);
      setStatus('正在获取需要翻译的正文，请稍候...', '');
      const revision = translationRevision;
      const payload = getBasePayload();
      const requestedTweetId = tweetIdFromUrl(payload.url);

      try {
        const response = await fetch('/api/preview-translations', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });

        const data = await response.json().catch(() => ({}));
        if (revision !== translationRevision) {
          setStatus('链接或选项已改变，请重新获取翻译。');
          return;
        }
        if (!response.ok || !data.ok) {
          throw new Error(data.error || '获取翻译内容失败');
        }

        if (!requestedTweetId || String(data.tweetId) !== requestedTweetId) {
          throw new Error('翻译结果与当前推文不一致，请重新获取翻译');
        }
        translationTweetId = requestedTweetId;
        translationDraft = (data.items || []).map((item) => ({
          index: item.index,
          label: item.label,
          originalText: item.originalText || '',
          translation: item.translation || ''
        }));
        translationPrepared = true;
        renderTranslationReview();
        translateBtn.textContent = '重新获取翻译';
        setStatus(
          translationDraft.length
            ? '翻译内容已经准备好了。现在点击“开始截图”会把这里的中文带进最终图片。'
            : '没有检测到需要翻译的正文。现在直接点击“开始截图”即可。',
          'success'
        );
      } catch (error) {
        if (revision !== translationRevision) return;
        resetTranslationReview();
        setStatus(error.message || '获取翻译内容失败', 'error');
      } finally {
        setBusy(false);
      }
    }

    async function capture() {
      if (busy) return;
      if (translationPrepared && translationTweetId !== tweetIdFromUrl(input.value.trim())) resetTranslationReview();
      const url = input.value.trim();
      if (!url) {
        setStatus('请输入推文链接', 'error');
        return;
      }

      setBusy(true);
      setStatus(
        translationPrepared ? '正在生成带翻译的截图，请稍候...' : '正在生成截图，请稍候...',
        ''
      );

      try {
        const payload = getBasePayload();
        payload.translateBody = translationPrepared;
        if (translationPrepared) {
          payload.translationOverrides = translationDraft.map((item) => ({
            index: item.index,
            translation: item.translation || ''
          }));
        }

        const data = await requestCapture(payload);
        updateResult(data);
        setStatus(data.message || '截图已保存', 'success');
      } catch (error) {
        setStatus(error.message || '截图失败', 'error');
      } finally {
        setBusy(false);
      }
    }

    translateBtn.addEventListener('click', prepareTranslations);
    captureBtn.addEventListener('click', capture);
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        capture();
      }
    });

    [input, showBrowser, anonymous, darkMode].forEach((element) => {
      element.addEventListener('input', resetTranslationReview);
      element.addEventListener('change', resetTranslationReview);
    });
