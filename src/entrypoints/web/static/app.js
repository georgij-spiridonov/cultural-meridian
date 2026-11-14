/* Клиентские улучшения доступности, копирования ссылок, валидации форм и диалогов */

(function initializeCulturalMeridianUi() {
  const announcer = document.getElementById("live-announcer");

  function announce(message) {
    if (!announcer || !message) {
      return;
    }
    announcer.textContent = "";
    window.setTimeout(function () {
      announcer.textContent = message;
    }, 30);
  }

  /* Синхронизация :user-invalid с aria-invalid и aria-describedby */
  function syncFieldValidity(field) {
    if (!(field instanceof HTMLElement)) {
      return true;
    }
    const supportsValidity =
      field instanceof HTMLInputElement ||
      field instanceof HTMLTextAreaElement ||
      field instanceof HTMLSelectElement;
    if (!supportsValidity) {
      return true;
    }

    const errorId = field.getAttribute("data-error");
    const errorNode = errorId ? document.getElementById(errorId) : null;
    const isInvalid = !field.checkValidity();

    if (field.matches(":user-invalid")) {
      field.setAttribute("aria-invalid", "true");
      if (errorNode) {
        errorNode.hidden = false;
        if (!errorNode.textContent || !errorNode.textContent.trim()) {
          errorNode.textContent = field.validationMessage || "Проверьте правильность заполнения поля.";
        }
        field.setAttribute("aria-describedby", errorId);
      }
    } else if (!isInvalid) {
      field.setAttribute("aria-invalid", "false");
      if (errorNode) {
        errorNode.hidden = true;
      }
      field.removeAttribute("aria-describedby");
    }
    return !isInvalid;
  }

  document.querySelectorAll("form").forEach(function (form) {
    const validatedControls = form.querySelectorAll("input[data-error], textarea[data-error], select[data-error]");
    validatedControls.forEach(function (control) {
      control.setAttribute("aria-invalid", "false");
      control.addEventListener("blur", function () {
        syncFieldValidity(control);
      });
      control.addEventListener("input", function () {
        if (control.getAttribute("aria-invalid") === "true") {
          syncFieldValidity(control);
        }
      });
      control.addEventListener("invalid", function () {
        control.setAttribute("aria-invalid", "true");
        const errorId = control.getAttribute("data-error");
        const errorNode = errorId ? document.getElementById(errorId) : null;
        if (errorNode) {
          errorNode.hidden = false;
          errorNode.textContent = control.validationMessage || "Заполните это поле корректно.";
          control.setAttribute("aria-describedby", errorId);
        }
      });
    });

    form.addEventListener("submit", function (event) {
      if (typeof form.checkValidity === "function" && !form.checkValidity()) {
        event.preventDefault();
        const firstInvalid = form.querySelector(":invalid");
        if (firstInvalid instanceof HTMLElement) {
          firstInvalid.setAttribute("aria-invalid", "true");
          firstInvalid.focus();
          announce("Проверьте выделенное поле формы перед отправкой.");
        }
      }
    });
  });

  /* Копирование ссылки на текущую подборку или виджет */
  document.addEventListener("click", function (event) {
    const target = event.target instanceof Element ? event.target.closest("[data-copy-action]") : null;
    if (!target) {
      return;
    }

    const mode = target.getAttribute("data-copy-action");
    let textToCopy = window.location.href;
    if (mode === "snippet") {
      const sourceId = target.getAttribute("data-copy-source");
      const sourceElement = sourceId ? document.getElementById(sourceId) : null;
      if (sourceElement) {
        textToCopy = sourceElement.textContent || "";
      }
    }

    if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
      navigator.clipboard.writeText(textToCopy).then(function () {
        const feedbackText =
          mode === "snippet"
            ? "HTML-код встраивания виджета скопирован в буфер обмена."
            : "Ссылка на подборку скопирована. Ею можно поделиться.";
        announce(feedbackText);
        const originalLabel = target.textContent;
        target.textContent = "Скопировано";
        window.setTimeout(function () {
          target.textContent = originalLabel;
        }, 2000);
      });
    }
  });

  /* Фоллбэк light-dismiss для браузеров без атрибута closedby="any" */
  document.querySelectorAll('dialog[closedby="any"]').forEach(function (dialog) {
    if (!("closedBy" in HTMLDialogElement.prototype)) {
      dialog.addEventListener("click", function (event) {
        if (event.target === dialog && typeof dialog.close === "function") {
          dialog.close();
        }
      });
    }
  });

  /* Открытие диалога подтверждения отмены события в админке */
  document.addEventListener("click", function (event) {
    const trigger = event.target instanceof Element ? event.target.closest("[data-open-cancel-dialog]") : null;
    if (!trigger) {
      return;
    }
    const dialog = document.getElementById("cancel-event-dialog");
    const form = document.getElementById("cancel-event-form");
    const titleTarget = document.getElementById("cancel-event-title");
    if (!(dialog instanceof HTMLDialogElement) || !(form instanceof HTMLFormElement)) {
      return;
    }

    const actionUrl = trigger.getAttribute("data-cancel-url") || "";
    const eventTitle = trigger.getAttribute("data-event-title") || "";
    form.setAttribute("action", actionUrl);
    if (titleTarget) {
      titleTarget.textContent = eventTitle;
    }
    dialog.showModal();
  });

  /* Разрешаем HTMX подмену фрагмента веб-чата при ошибках валидации (422) и rate limit (429) */
  document.body.addEventListener("htmx:beforeSwap", function (event) {
    const detail = event.detail;
    if (!detail || !detail.target || detail.target.id !== "assistant-chat" || !detail.xhr) {
      return;
    }
    const statusCode = detail.xhr.status;
    if (statusCode === 422 || statusCode === 429) {
      detail.shouldSwap = true;
      detail.isError = false;
    }
  });

  /* Объявление результатов HTMX в live-region и обновление состояния веб-чата */
  document.body.addEventListener("htmx:afterSwap", function (event) {
    const detailTarget = event.detail && event.detail.target;
    if (!detailTarget) {
      return;
    }

    if (detailTarget.id === "catalog-results") {
      const countNode = detailTarget.querySelector("[data-results-count]");
      if (countNode) {
        announce(countNode.textContent || "Выдача мероприятий обновлена.");
      }
      return;
    }

    if (detailTarget.id === "assistant-chat") {
      const errorNode = detailTarget.querySelector('[data-assistant-status="error"]');
      const noticeNode = detailTarget.querySelector('[data-assistant-status="notice"]');
      const answerNode = detailTarget.querySelector("[data-assistant-answer-text]");
      const clarifyingNode = detailTarget.querySelector("[data-assistant-clarifying]");
      const pendingNode = detailTarget.querySelector("[data-pending-action-id]");
      const messagesList = detailTarget.querySelector("[data-assistant-messages]");

      if (messagesList) {
        messagesList.scrollTop = messagesList.scrollHeight;
      }

      if (errorNode) {
        announce(errorNode.textContent || "Ошибка при обращении к ассистенту.");
        return;
      }

      const composerInput = document.getElementById("assistant-user-message");
      const requestElt = event.detail && event.detail.elt;
      if (
        composerInput instanceof HTMLTextAreaElement &&
        requestElt instanceof Element &&
        requestElt.closest("[data-assistant-turn-form], [data-assistant-reset-form]")
      ) {
        composerInput.value = "";
        composerInput.setAttribute("aria-invalid", "false");
        composerInput.removeAttribute("aria-describedby");
        const composerError = document.getElementById("assistant-user-message-error");
        if (composerError) {
          composerError.hidden = true;
        }
      }

      if (pendingNode) {
        announce("Требуется подтверждение действия. Используйте кнопки «Подтвердить» или «Отменить».");
      } else if (answerNode && answerNode.textContent && answerNode.textContent.trim()) {
        const prefix = clarifyingNode ? "Новый ответ и уточняющий вопрос ассистента: " : "Ответ ассистента: ";
        announce(prefix + answerNode.textContent.trim());
      } else if (noticeNode && noticeNode.textContent && noticeNode.textContent.trim()) {
        announce(noticeNode.textContent.trim());
      } else {
        announce("Состояние диалога с ассистентом обновлено.");
      }
    }
  });

  /* Клавиатурная навигация по быстрым подсказкам и отправка по Enter в чате /assistant */
  const assistantForm = document.querySelector("[data-assistant-turn-form]");
  const assistantInput = document.getElementById("assistant-user-message");
  if (assistantForm instanceof HTMLFormElement && assistantInput instanceof HTMLTextAreaElement) {
    assistantInput.addEventListener("keydown", function (event) {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        if (typeof assistantForm.requestSubmit === "function") {
          assistantForm.requestSubmit();
        } else {
          assistantForm.submit();
        }
      }
    });
  }

  const promptsList = document.querySelector("[data-assistant-prompts-list]");
  if (promptsList) {
    promptsList.addEventListener("keydown", function (event) {
      const currentChip =
        event.target instanceof Element ? event.target.closest("[data-assistant-prompt]") : null;
      if (!(currentChip instanceof HTMLElement)) {
        return;
      }
      const chips = Array.from(promptsList.querySelectorAll("[data-assistant-prompt]")).filter(
        function (node) {
          return node instanceof HTMLElement;
        }
      );
      if (chips.length === 0) {
        return;
      }
      const currentIndex = chips.indexOf(currentChip);
      let nextIndex = -1;
      if (event.key === "ArrowRight" || event.key === "ArrowDown") {
        nextIndex = (currentIndex + 1) % chips.length;
      } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
        nextIndex = (currentIndex - 1 + chips.length) % chips.length;
      } else if (event.key === "Home") {
        nextIndex = 0;
      } else if (event.key === "End") {
        nextIndex = chips.length - 1;
      }
      if (nextIndex >= 0) {
        event.preventDefault();
        chips[nextIndex].focus();
      }
    });
  }

  /* Интерактивный конструктор виджета на /widget/demo */
  const demoForm = document.getElementById("widget-config-form");
  const demoMount = document.getElementById("widget-demo-mount");
  const snippetOutput = document.getElementById("widget-embed-snippet");
  if (demoForm instanceof HTMLFormElement && demoMount && snippetOutput) {
    const baseUrl = demoMount.getAttribute("data-base-url") || window.location.origin;

    function rebuildWidgetPreview() {
      const formData = new FormData(demoForm);
      const city = String(formData.get("city") || "").trim();
      const category = String(formData.get("category") || "").trim();
      const period = String(formData.get("period") || "").trim();
      const limit = String(formData.get("limit") || "5").trim();
      const height = String(formData.get("height") || "540").trim();

      demoMount.setAttribute("data-city", city);
      demoMount.setAttribute("data-category", category);
      demoMount.setAttribute("data-period", period);
      demoMount.setAttribute("data-limit", limit);
      demoMount.setAttribute("data-height", height);

      if (typeof window.mountCulturalMeridianWidgets === "function") {
        window.mountCulturalMeridianWidgets(demoMount);
      }

      const attributes = [
        'data-cultural-meridian-widget="true"',
        city ? `data-city="${city}"` : "",
        category ? `data-category="${category}"` : "",
        period ? `data-period="${period}"` : "",
        `data-limit="${limit}"`,
        `data-height="${height}"`,
      ]
        .filter(Boolean)
        .join("\n  ");

      snippetOutput.textContent =
        `<div\n  ${attributes}\n></div>\n<script src="${baseUrl}/widget.js" defer></script>`;
      announce("Предпросмотр встраиваемого виджета обновлён.");
    }

    demoForm.addEventListener("change", rebuildWidgetPreview);
    demoForm.addEventListener("submit", function (event) {
      event.preventDefault();
      rebuildWidgetPreview();
    });
  }
})();
