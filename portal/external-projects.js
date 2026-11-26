(function () {
  'use strict';

  var container = document.getElementById('external-projects');
  var error = document.getElementById('external-error');
  var refreshMs = 30000;
  var timer;

  function element(tag, text, className) {
    var node = document.createElement(tag);
    if (text != null) node.textContent = text;
    if (className) node.className = className;
    return node;
  }

  function render(projects) {
    container.textContent = '';
    if (!projects.length) {
      container.appendChild(element('p', 'No hay proyectos activos.', 'text-body-secondary'));
      return;
    }
    var groups = new Map();
    projects.forEach(function (project) {
      if (!groups.has(project.company)) groups.set(project.company, []);
      groups.get(project.company).push(project);
    });
    groups.forEach(function (items, company) {
      var section = element('section');
      section.appendChild(element('h2', company, 'h5 mt-4'));
      var table = element('table', null, 'table table-hover align-middle links');
      var head = element('thead');
      var header = element('tr');
      ['Proyecto', 'URL', 'Estado', 'Último chequeo'].forEach(function (label) {
        var th = element('th', label);
        th.scope = 'col';
        header.appendChild(th);
      });
      head.appendChild(header);
      table.appendChild(head);
      var body = element('tbody');
      items.forEach(function (project) {
        var row = element('tr');
        var name = element('td', null, 'name');
        name.setAttribute('data-th', 'Proyecto');
        var iconUrl = project.icon && /^[a-zA-Z0-9_-]+\.(png|svg)$/i.test(project.icon)
          ? '/favicons/' + project.icon
          : project.icon_url;
        if (iconUrl && /^\/api\/external-projects\/\d+\/icon$|^\/favicons\/[a-zA-Z0-9_-]+\.(png|svg)$/i.test(iconUrl)) {
          var icon = element('img', null, 'app-icon');
          icon.src = iconUrl;
          icon.alt = '';
          icon.width = 18;
          icon.height = 18;
          icon.loading = 'lazy';
          icon.onerror = function () { this.remove(); };
          name.appendChild(icon);
        }
        name.appendChild(document.createTextNode(project.name));
        row.appendChild(name);
        var urlCell = element('td', null, 'url');
        urlCell.setAttribute('data-th', 'URL');
        try {
          var parsed = new URL(project.url);
          if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') throw new Error('URL inválida');
          var link = element('a', project.url);
          link.href = parsed.href;
          link.target = '_blank';
          link.rel = 'noopener noreferrer';
          urlCell.appendChild(link);
        } catch (e) {
          urlCell.textContent = project.url + ' (URL inválida)';
        }
        row.appendChild(urlCell);
        var stateCell = element('td', null, 'status');
        stateCell.setAttribute('data-th', 'Estado');
        var classes = { online: 'ok', auth: 'login', offline: 'err', unknown: 'unknown' };
        var labels = { online: 'Disponible', auth: 'Autenticación', offline: 'Caído', unknown: 'Pendiente' };
        var pill = element('span', labels[project.state] || 'Pendiente',
          'badge rounded-pill pill-' + (classes[project.state] || 'unknown'));
        if (project.error) pill.title = project.error;
        stateCell.appendChild(pill);
        if (project.code != null) stateCell.appendChild(element('span', ' HTTP ' + project.code, 'meta'));
        if (project.latency_ms != null) stateCell.appendChild(element('span', ' · ' + project.latency_ms + ' ms', 'meta'));
        if (project.error) stateCell.appendChild(element('div', project.error, 'small text-body-secondary'));
        row.appendChild(stateCell);
        var checked = element('td', project.checked_at ? new Date(project.checked_at).toLocaleString() : '—');
        checked.setAttribute('data-th', 'Último chequeo');
        row.appendChild(checked);
        body.appendChild(row);
      });
      table.appendChild(body);
      section.appendChild(table);
      container.appendChild(section);
    });
  }

  function load() {
    clearTimeout(timer);
    fetch('/api/external-projects', { cache: 'no-store' })
      .then(function (response) {
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return response.json();
      })
      .then(function (data) {
        render(data.projects);
        refreshMs = data.refresh_ms || 30000;
        error.hidden = !data.last_error;
        error.textContent = data.last_error ? 'Falló el último ciclo de comprobación: ' + data.last_error : '';
      })
      .catch(function () {
        error.hidden = false;
        error.textContent = 'No se pudo consultar el estado de los proyectos. Se reintentará.';
      })
      .finally(function () { timer = setTimeout(load, refreshMs); });
  }
  load();
})();
