/* Изолированный скрипт встраивания афиши «Культурный Меридиан» без сторонних cookies */

(function (globalObject, documentObject) {
  function resolveBaseUrl(container, scriptElement) {
    const explicitBase = container.getAttribute("data-base-url");
    if (explicitBase) {
      return explicitBase.replace(/\/+$/, "");
    }
    if (scriptElement && scriptElement.src) {
      try {
        return new URL(scriptElement.src).origin;
      } catch {
        return globalObject.location.origin;
      }
    }
    return globalObject.location.origin;
  }

  function sanitizeHeight(rawHeight) {
    const parsed = Number.parseInt(rawHeight || "520", 10);
    if (!Number.isFinite(parsed)) {
      return 520;
    }
    return Math.max(280, Math.min(1200, parsed));
  }

  function buildWidgetSourceUrl(container, baseUrl) {
    const queryParameters = new URLSearchParams();
    const mapping = {
      city: "data-city",
      category: "data-category",
      period: "data-period",
      limit: "data-limit",
      height: "data-height",
    };

    Object.keys(mapping).forEach(function (parameterName) {
      const attributeValue = container.getAttribute(mapping[parameterName]);
      if (attributeValue && attributeValue.trim()) {
        queryParameters.set(parameterName, attributeValue.trim());
      }
    });

    const queryString = queryParameters.toString();
    return queryString ? `${baseUrl}/widget?${queryString}` : `${baseUrl}/widget`;
  }

  function mountSingleWidget(container, scriptElement) {
    if (!(container instanceof HTMLElement)) {
      return;
    }
    const baseUrl = resolveBaseUrl(container, scriptElement);
    const frameHeight = sanitizeHeight(container.getAttribute("data-height"));
    const widgetTitle = container.getAttribute("data-title") || "Афиша культуры Республики Башкортостан";
    const sourceUrl = buildWidgetSourceUrl(container, baseUrl);

    const shadowRoot = container.shadowRoot || container.attachShadow({ mode: "open" });
    shadowRoot.innerHTML = "";

    const styleElement = documentObject.createElement("style");
    styleElement.textContent = `
      :host {
        display: block;
        all: initial;
        width: 100%;
        font-family: system-ui, -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
      }
      .cm-widget-shell {
        width: 100%;
        border: 1px solid #cfd8d3;
        border-radius: 14px;
        overflow: hidden;
        background: #ffffff;
        box-shadow: 0 2px 10px rgba(20, 32, 29, 0.06);
      }
      .cm-widget-frame {
        display: block;
        width: 100%;
        height: ${frameHeight}px;
        border: 0;
        background: #f5f4ee;
      }
    `;

    const shell = documentObject.createElement("div");
    shell.className = "cm-widget-shell";

    const iframe = documentObject.createElement("iframe");
    iframe.className = "cm-widget-frame";
    iframe.src = sourceUrl;
    iframe.title = widgetTitle;
    iframe.loading = "lazy";
    iframe.referrerPolicy = "strict-origin-when-cross-origin";

    shell.appendChild(iframe);
    shadowRoot.appendChild(styleElement);
    shadowRoot.appendChild(shell);
  }

  function mountCulturalMeridianWidgets(targetElement) {
    const activeScript = documentObject.currentScript;
    if (targetElement instanceof HTMLElement) {
      mountSingleWidget(targetElement, activeScript);
      return;
    }

    const containers = documentObject.querySelectorAll("[data-cultural-meridian-widget]");
    if (containers.length > 0) {
      containers.forEach(function (node) {
        mountSingleWidget(node, activeScript);
      });
      return;
    }

    if (activeScript && activeScript.parentNode) {
      const autoContainer = documentObject.createElement("div");
      autoContainer.setAttribute("data-cultural-meridian-widget", "true");
      ["data-city", "data-category", "data-period", "data-limit", "data-height", "data-title"].forEach(function (attr) {
        const value = activeScript.getAttribute(attr);
        if (value) {
          autoContainer.setAttribute(attr, value);
        }
      });
      activeScript.parentNode.insertBefore(autoContainer, activeScript);
      mountSingleWidget(autoContainer, activeScript);
    }
  }

  globalObject.mountCulturalMeridianWidgets = mountCulturalMeridianWidgets;

  if (documentObject.readyState === "loading") {
    documentObject.addEventListener("DOMContentLoaded", function () {
      mountCulturalMeridianWidgets();
    });
  } else {
    mountCulturalMeridianWidgets();
  }
})(window, document);
