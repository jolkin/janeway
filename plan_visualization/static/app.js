/**
 * Plan Execution Visualization
 *
 * Renders two views from a Kirk plan JSON + execution schedule:
 *   1. Execution Timeline — events placed at their actual execution times
 *   2. Temporal Constraint Graph — events as nodes, constraints as directed edges
 */

// ── State ──────────────────────────────────────────────────────────────────────
let graphData = null;
let graphZoom = null;
let liveWs = null;
// Accumulated state updates from the causal link monitor
let stateUpdates = [];
// Set of causal link labels that have been violated (for re-rendering)
let violatedLinks = new Set();

// ── Color palette ──────────────────────────────────────────────────────────────
const COLORS = {
  start:        "#4caf50",
  end:          "#f44336",
  action_start: "#2196f3",
  action_end:   "#ff9800",
  event:        "#9c27b0",
  edge:         "#555",
  causal:       "#ffd700",
  violation:    "#ff1744",
  episode:      [
    "#e94560", "#0f3460", "#2196f3", "#4caf50",
    "#ff9800", "#9c27b0", "#00bcd4", "#ff5722",
    "#8bc34a", "#673ab7", "#009688", "#ffc107",
  ],
};

function nodeColor(type) {
  return COLORS[type] || COLORS.event;
}

// ── File input handling ────────────────────────────────────────────────────────
const fileInput = document.getElementById("file-input");
const btnLoad   = document.getElementById("btn-load");
const statusEl  = document.getElementById("status");

let pendingFile = null;

fileInput.addEventListener("change", () => {
  pendingFile = fileInput.files[0] || null;
  btnLoad.disabled = !pendingFile;
});

async function fetchAndRenderGraph() {
  try {
    const resp = await fetch("/graph");
    if (!resp.ok) return false;
    graphData = await resp.json();
    statusEl.textContent = `Loaded: ${graphData.nodes.length} events, ${graphData.edges.length} constraints`;
    renderTimeline(graphData);
    renderGraph(graphData);
    return true;
  } catch (err) {
    console.error("Failed to fetch graph:", err);
    return false;
  }
}

btnLoad.addEventListener("click", async () => {
  if (!pendingFile) return;
  statusEl.textContent = "Loading...";
  try {
    const text = await pendingFile.text();
    const json = JSON.parse(text);

    // The file can be either {plan, executions} or just the raw plan
    let plan, executions;
    if (json.plan) {
      plan = json.plan;
      executions = json.executions || [];
    } else {
      plan = json;
      executions = [];
    }

    const resp = await fetch("/load", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plan, executions }),
    });
    if (!resp.ok) {
      const err = await resp.json();
      throw new Error(err.detail || resp.statusText);
    }

    await fetchAndRenderGraph();
  } catch (err) {
    statusEl.textContent = `Error: ${err.message}`;
    console.error(err);
  }
});

// Try to load an existing graph from the backend on page load
fetchAndRenderGraph();

// ── Tooltip ────────────────────────────────────────────────────────────────────
const tooltip = document.getElementById("tooltip");

function showTooltip(evt, html) {
  tooltip.innerHTML = html;
  tooltip.style.display = "block";
  tooltip.style.left = (evt.clientX + 12) + "px";
  tooltip.style.top  = (evt.clientY + 12) + "px";
}

function hideTooltip() {
  tooltip.style.display = "none";
}

// ── Timeline Rendering ────────────────────────────────────────────────────────
function renderTimeline(data) {
  const container = document.getElementById("timeline-container");
  const svg = d3.select("#timeline-svg");
  svg.selectAll("*").remove();

  const width  = container.clientWidth;
  const height = container.clientHeight;
  const margin = { top: 30, right: 40, bottom: 35, left: 40 };
  const innerW = width - margin.left - margin.right;
  const innerH = height - margin.top - margin.bottom;

  svg.attr("width", width).attr("height", height);

  const g = svg.append("g")
    .attr("transform", `translate(${margin.left},${margin.top})`);

  // Time scale — use execution times if available, else spread events evenly
  const executedNodes = data.nodes.filter(n => n.executionTime != null);
  const hasExecution = executedNodes.length > 0;

  let xScale;
  if (hasExecution) {
    const tMin = d3.min(executedNodes, d => d.executionTime);
    const tMax = d3.max(executedNodes, d => d.executionTime);
    const pad = (tMax - tMin) * 0.05 || 1;
    xScale = d3.scaleLinear()
      .domain([tMin - pad, tMax + pad])
      .range([0, innerW]);
  } else {
    // Place nodes evenly based on topological ordering
    xScale = d3.scaleLinear()
      .domain([0, data.nodes.length - 1])
      .range([0, innerW]);
  }

  // Time axis
  g.append("g")
    .attr("class", "axis")
    .attr("transform", `translate(0,${innerH})`)
    .call(d3.axisBottom(xScale).ticks(Math.min(data.nodes.length, 15)))
    .append("text")
    .attr("x", innerW / 2)
    .attr("y", 28)
    .attr("fill", "#999")
    .attr("text-anchor", "middle")
    .text(hasExecution ? "Execution Time (s)" : "Event Index");

  // Draw episode bars (grouped by activity)
  const episodeColorMap = {};
  let colorIdx = 0;
  data.episodes.forEach(ep => {
    if (!episodeColorMap[ep.activityName]) {
      episodeColorMap[ep.activityName] = COLORS.episode[colorIdx % COLORS.episode.length];
      colorIdx++;
    }
  });

  // Group episodes into rows to avoid overlap
  const episodeRows = [];
  const sortedEpisodes = [...data.episodes].sort((a, b) => {
    const aStart = getNodeX(a.startEvent);
    const bStart = getNodeX(b.startEvent);
    return aStart - bStart;
  });

  function getNodeX(eventId) {
    if (hasExecution) {
      const n = data.nodes.find(n => n.id === eventId);
      return n && n.executionTime != null ? xScale(n.executionTime) : 0;
    }
    const idx = data.nodes.findIndex(n => n.id === eventId);
    return xScale(Math.max(0, idx));
  }

  for (const ep of sortedEpisodes) {
    const x1 = getNodeX(ep.startEvent);
    const x2 = getNodeX(ep.endEvent);
    let row = 0;
    while (episodeRows[row] && episodeRows[row] > x1 - 5) {
      row++;
    }
    ep._row = row;
    ep._x1 = x1;
    ep._x2 = x2;
    episodeRows[row] = x2;
  }

  const barHeight = 20;
  const barGap = 4;
  const barsY = 10;

  g.selectAll(".episode-bar")
    .data(sortedEpisodes)
    .enter()
    .append("rect")
    .attr("class", "episode-bar")
    .attr("x", d => d._x1)
    .attr("y", d => barsY + d._row * (barHeight + barGap))
    .attr("width", d => Math.max(d._x2 - d._x1, 4))
    .attr("height", barHeight)
    .attr("fill", d => episodeColorMap[d.activityName])
    .on("mouseover", (evt, d) => {
      showTooltip(evt,
        `<span class="label">${d.activityName}</span><br>` +
        `Duration: [${d.durationLB}, ${d.durationUB}]<br>` +
        `${d.startEvent} &rarr; ${d.endEvent}`
      );
    })
    .on("mousemove", (evt) => {
      tooltip.style.left = (evt.clientX + 12) + "px";
      tooltip.style.top  = (evt.clientY + 12) + "px";
    })
    .on("mouseout", hideTooltip);

  g.selectAll(".episode-label")
    .data(sortedEpisodes.filter(d => (d._x2 - d._x1) > 40))
    .enter()
    .append("text")
    .attr("class", "episode-label")
    .attr("x", d => (d._x1 + d._x2) / 2)
    .attr("y", d => barsY + d._row * (barHeight + barGap) + barHeight / 2 + 4)
    .attr("text-anchor", "middle")
    .text(d => d.activityName);

  // Draw event markers on the timeline
  const eventY = innerH - 20;

  const events = g.selectAll(".timeline-event")
    .data(data.nodes)
    .enter()
    .append("g")
    .attr("class", "timeline-event")
    .attr("transform", (d, i) => {
      const x = hasExecution && d.executionTime != null
        ? xScale(d.executionTime)
        : xScale(i);
      return `translate(${x},${eventY})`;
    });

  //events.append("circle")
  //  .attr("r", 6)
  //  .attr("fill", d => nodeColor(d.type))
  //  .attr("stroke", "#fff");

  //events.append("text")
  //  .attr("dy", -10)
  //  .attr("text-anchor", "middle")
  //  .text(d => d.label.length > 18 ? d.label.slice(0, 16) + ".." : d.label);

  // Vertical reference lines from events to episode bars
  events.each(function(d, i) {
    const x = hasExecution && d.executionTime != null
      ? xScale(d.executionTime)
      : xScale(i);
    g.append("line")
      .attr("x1", x).attr("x2", x)
      .attr("y1", barsY + (episodeRows.length) * (barHeight + barGap))
      .attr("y2", eventY - 8)
      .attr("stroke", "#333")
      .attr("stroke-dasharray", "2,3");
  });

  // Event tooltips
  events.on("mouseover", (evt, d) => {
    let html = `<span class="label">${d.id}</span>`;
    if (d.activity) html += `<br>Activity: ${d.activity}`;
    if (d.executionTime != null) html += `<br>Executed at: ${d.executionTime.toFixed(3)}s`;
    else html += `<br><em>No execution time</em>`;
    showTooltip(evt, html);
  })
  .on("mousemove", (evt) => {
    tooltip.style.left = (evt.clientX + 12) + "px";
    tooltip.style.top  = (evt.clientY + 12) + "px";
  })
  .on("mouseout", hideTooltip);

  // Re-render any accumulated state update markers
  stateUpdates.forEach((msg, i) => addStateUpdateMarker(msg, i));
}

// ── Graph Rendering (d3-dag sugiyama, left-to-right) ─────────────────────────
function renderGraph(data) {
  const container = document.getElementById("graph-container");
  const svg = d3.select("#graph-svg");
  svg.selectAll("*").remove();

  const width  = container.clientWidth;
  const height = container.clientHeight;

  svg.attr("width", width).attr("height", height);

  // Arrowhead markers
  const defs = svg.append("defs");
  [
    { id: "arrow",         color: "#777" },
    { id: "arrow-causal",  color: COLORS.causal },
    { id: "arrow-episode", color: "#4caf50" },
    { id: "arrow-violated", color: COLORS.violation },
  ].forEach(({ id, color }) => {
    defs.append("marker")
      .attr("id", id)
      .attr("viewBox", "0 -5 10 10")
      .attr("refX", 18)
      .attr("refY", 0)
      .attr("markerWidth", 6)
      .attr("markerHeight", 6)
      .attr("orient", "auto")
      .append("path")
      .attr("d", "M0,-5L10,0L0,5")
      .attr("fill", color);
  });

  const g = svg.append("g");

  // Zoom
  graphZoom = d3.zoom()
    .scaleExtent([0.1, 5])
    .on("zoom", (evt) => g.attr("transform", evt.transform));
  svg.call(graphZoom);

  // ── Build DAG for layout ───────────────────────────────────────────────────
  const nodeIds = new Set(data.nodes.map(n => n.id));

  // Build unique parent relationships for d3-dag (deduped per target)
  const parentMap = new Map();
  for (const n of data.nodes) parentMap.set(n.id, new Set());
  for (const e of data.edges) {
    if (nodeIds.has(e.source) && nodeIds.has(e.target) && e.source !== e.target) {
      parentMap.get(e.target).add(e.source);
    }
  }

  const stratData = data.nodes.map(n => ({
    id: n.id,
    parentIds: [...parentMap.get(n.id)],
  }));

  // Compute positions using sugiyama (top-to-bottom), then swap for L-to-R
  let nodePositions;
  try {
    const dag = d3.graphStratify()(stratData);
    const layout = d3.sugiyama()
      .nodeSize([40, 40])
      .gap([20, 80]);
    layout(dag);

    nodePositions = new Map();
    for (const node of dag.nodes()) {
      // Swap x ↔ y so layers flow left-to-right instead of top-to-bottom
      nodePositions.set(node.data.id, { x: node.y, y: node.x });
    }

    // Ensure the "end" node is placed at the rightmost position
    const endNode = data.nodes.find(n => n.type === "end");
    console.log("End node:", endNode);
    if (endNode && nodePositions.has(endNode.id)) {
      const maxX = Math.max(...[...nodePositions.values()].map(p => p.x));
      const endPos = nodePositions.get(endNode.id);
      if (endPos.x < maxX) {
        endPos.x = maxX + 80;
      }
    }
  } catch (err) {
    console.error("d3-dag layout failed:", err);
    // Fallback: arrange nodes in a simple grid
    nodePositions = new Map();
    data.nodes.forEach((n, i) => {
      const cols = Math.ceil(Math.sqrt(data.nodes.length));
      nodePositions.set(n.id, {
        x: (i % cols) * 150,
        y: Math.floor(i / cols) * 80,
      });
    });
  }

  // ── Fit to viewport ────────────────────────────────────────────────────────
  const margin = { top: 40, right: 100, bottom: 40, left: 40 };
  const positions = Array.from(nodePositions.values());
  const xExt = d3.extent(positions, p => p.x);
  const yExt = d3.extent(positions, p => p.y);
  const dagW = (xExt[1] - xExt[0]) || 1;
  const dagH = (yExt[1] - yExt[0]) || 1;
  const innerW = width - margin.left - margin.right;
  const innerH = height - margin.top - margin.bottom;
  const scale = Math.min(innerW / dagW, innerH / dagH, 2.0);
  const offsetX = margin.left + (innerW - dagW * scale) / 2 - xExt[0] * scale;
  const offsetY = margin.top  + (innerH - dagH * scale) / 2 - yExt[0] * scale;

  const initTransform = d3.zoomIdentity.translate(offsetX, offsetY).scale(scale);
  svg.call(graphZoom.transform, initTransform);

  // ── Node data with positions ───────────────────────────────────────────────
  const nodesWithPos = data.nodes.map(n => {
    const pos = nodePositions.get(n.id) || { x: 0, y: 0 };
    return { ...n, x: pos.x, y: pos.y };
  });
  const nodeMap = new Map(nodesWithPos.map(n => [n.id, n]));

  // ── Edge offset for parallel edges between same pair ───────────────────────
  const pairCount = new Map();
  for (const e of data.edges) {
    const key = `${e.source}->${e.target}`;
    pairCount.set(key, (pairCount.get(key) || 0) + 1);
  }
  const pairIdx = new Map();
  const edgesWithMeta = data.edges.map(e => {
    const key = `${e.source}->${e.target}`;
    const idx = pairIdx.get(key) || 0;
    pairIdx.set(key, idx + 1);
    const count = pairCount.get(key);
    return { ...e, _offset: (idx - (count - 1) / 2) * 10 };
  });

  // Horizontal link generator
  const linkGen = d3.linkHorizontal().x(d => d[0]).y(d => d[1]);

  // ── Render edges ───────────────────────────────────────────────────────────
  const edgeGroups = g.selectAll(".edge")
    .data(edgesWithMeta)
    .enter()
    .append("g")
    .attr("class", d => {
      let cls = "edge";
      if (d.hasCausalLink) cls += " causal";
      if (d.isEpisode) cls += " episode";
      return cls;
    });

  // Invisible wide hit area for easier hover
  edgeGroups.append("path")
    .attr("d", d => {
      const src = nodeMap.get(d.source);
      const tgt = nodeMap.get(d.target);
      if (!src || !tgt) return "";
      return linkGen({ source: [src.x, src.y + d._offset], target: [tgt.x, tgt.y + d._offset] });
    })
    .attr("stroke", "transparent")
    .attr("stroke-width", 12)
    .attr("fill", "none");

  // Visible edge path
  edgeGroups.append("path")
    .attr("class", "edge-path")
    .attr("d", d => {
      const src = nodeMap.get(d.source);
      const tgt = nodeMap.get(d.target);
      if (!src || !tgt) return "";
      return linkGen({ source: [src.x, src.y + d._offset], target: [tgt.x, tgt.y + d._offset] });
    })
    .attr("stroke", d => d.hasCausalLink ? COLORS.causal : d.isEpisode ? "#4caf50" : COLORS.edge)
    .attr("marker-end", d =>
      d.hasCausalLink ? "url(#arrow-causal)" : d.isEpisode ? "url(#arrow-episode)" : "url(#arrow)")
    .attr("fill", "none");

  // Edge labels at midpoint
  edgeGroups.append("text")
    .attr("class", "edge-label")
    .attr("text-anchor", "middle")
    .attr("dy", d => -4 + d._offset)
    .attr("x", d => {
      const src = nodeMap.get(d.source);
      const tgt = nodeMap.get(d.target);
      return src && tgt ? (src.x + tgt.x) / 2 : 0;
    })
    .attr("y", d => {
      const src = nodeMap.get(d.source);
      const tgt = nodeMap.get(d.target);
      return src && tgt ? (src.y + tgt.y) / 2 : 0;
    })
    .text(d => {
      let label = `[${d.lb}, ${d.ub}]`;
      if (d.causalLink) label += ` ${d.causalLink}`;
      return label;
    });

  // Edge tooltips
  edgeGroups.on("mouseover", (evt, d) => {
    let html = `<span class="label">${d.id}</span><br>` +
      `${d.source} &rarr; ${d.target}<br>` +
      `Bounds: [${d.lb}, ${d.ub}]`;
    if (d.causalLink) {
      html += `<br><span class="causal">Causal: ${d.causalLink}</span>`;
    }
    showTooltip(evt, html);
  })
  .on("mousemove", (evt) => {
    tooltip.style.left = (evt.clientX + 12) + "px";
    tooltip.style.top  = (evt.clientY + 12) + "px";
  })
  .on("mouseout", hideTooltip);

  // ── Render nodes ───────────────────────────────────────────────────────────
  const nodeGroups = g.selectAll(".node")
    .data(nodesWithPos)
    .enter()
    .append("g")
    .attr("class", "node")
    .attr("transform", d => `translate(${d.x},${d.y})`);

  nodeGroups.append("circle")
    .attr("r", 10)
    .attr("fill", d => nodeColor(d.type))
    .attr("stroke", "#fff");

  nodeGroups.append("text")
    .attr("text-anchor", "middle")
    .attr("dy", -16)
    .text(d => d.label.length > 22 ? d.label.slice(0, 20) + ".." : d.label);

  // Node tooltips
  nodeGroups.on("mouseover", (evt, d) => {
    let html = `<span class="label">${d.id}</span><br>Type: ${d.type}`;
    if (d.activity) html += `<br>Activity: ${d.activity}`;
    if (d.executionTime != null) html += `<br>Executed at: ${d.executionTime.toFixed(3)}s`;
    showTooltip(evt, html);
  })
  .on("mousemove", (evt) => {
    tooltip.style.left = (evt.clientX + 12) + "px";
    tooltip.style.top  = (evt.clientY + 12) + "px";
  })
  .on("mouseout", hideTooltip);

  // ── Controls ───────────────────────────────────────────────────────────────
  document.getElementById("toggle-causal").onchange = (evt) => {
    const show = evt.target.checked;
    edgeGroups.filter(d => d.hasCausalLink).classed("causal", show);
  };

  document.getElementById("toggle-plain-edges").onchange = (evt) => {
    const show = evt.target.checked;
    edgeGroups.filter(d => !d.hasCausalLink && !d.isEpisode)
      .style("display", show ? null : "none");
  };

  document.getElementById("btn-reset-zoom").onclick = () => {
    svg.transition().duration(500).call(graphZoom.transform, initTransform);
  };

  // Re-apply violated link highlighting from previous state updates
  for (const label of violatedLinks) {
    edgeGroups
      .filter(d => d.causalLink === label)
      .classed("violated", true)
      .selectAll(".edge-path")
      .attr("stroke", COLORS.violation)
      .attr("stroke-width", 3);
  }
}

// ── Live WebSocket updates ────────────────────────────────────────────────────
function connectLiveWs() {
  if (liveWs) {
    liveWs.close();
  }
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${proto}//${window.location.host}/ws`;
  liveWs = new WebSocket(url);

  liveWs.onopen = () => {
    console.log("Live WebSocket connected");
    statusEl.textContent = statusEl.textContent.replace(/ \| Live$/, "") + " | Live";
  };

  liveWs.onmessage = (evt) => {
    try {
      const msg = JSON.parse(evt.data);
      if (msg.type === "plan_loaded") {
        // Backend received a new plan — fetch and render it
        stateUpdates = [];
        violatedLinks = new Set();
        fetchAndRenderGraph();
        return;
      }
      if (!graphData) return;
      if (msg.type === "resync") {
        // Backend renormalized all times (e.g. start event arrived late)
        const nodeTimes = msg.nodeTimes || {};
        for (const node of graphData.nodes) {
          if (nodeTimes[node.id] !== undefined) {
            node.executionTime = nodeTimes[node.id];
          }
        }
        graphData.timelineEnd = msg.timelineEnd || graphData.timelineEnd;
        renderTimeline(graphData);
        return;
      }
      if (msg.type === "execution") {
        // Update the node in graphData
        const node = graphData.nodes.find(n => n.id === msg.event);
        if (node) {
          node.executionTime = msg.time;
        }
        // Update timeline end
        if (msg.time > graphData.timelineEnd) {
          graphData.timelineEnd = msg.time;
        }
        // Re-render timeline to reflect new execution time
        renderTimeline(graphData);
        // Flash the executed node in the graph
        highlightExecutedNode(msg.event);
      }
      if (msg.type === "state-update") {
        handleStateUpdate(msg);
      }
    } catch (err) {
      console.error("Live WS parse error:", err);
    }
  };

  liveWs.onclose = () => {
    console.log("Live WebSocket closed — reconnecting in 3s");
    setTimeout(connectLiveWs, 3000);
  };

  liveWs.onerror = (err) => {
    console.error("Live WebSocket error:", err);
    liveWs.close();
  };
}

function highlightExecutedNode(eventId) {
  // Pulse the node circle in the graph SVG
  d3.select("#graph-svg").selectAll(".node")
    .filter(d => d.id === eventId)
    .select("circle")
    .transition().duration(200)
    .attr("r", 16)
    .attr("stroke", "#fff")
    .attr("stroke-width", 3)
    .transition().duration(600)
    .attr("r", 10)
    .attr("stroke-width", 2);
}

// ── State update / violation handling ─────────────────────────────────────────
function handleStateUpdate(msg) {
  // msg: {type: "state-update", update: {var: val}, success: bool, violations: [...]}
  const idx = stateUpdates.length;
  stateUpdates.push(msg);

  // Add marker to the timeline
  addStateUpdateMarker(msg, idx);

  // If there are violations, highlight the causal link edges
  if (msg.violations && msg.violations.length > 0) {
    for (const v of msg.violations) {
      highlightViolatedEdge(v.causalLinkLabel);
    }
  }
}

function addStateUpdateMarker(msg, idx) {
  const svg = d3.select("#timeline-svg");
  const container = document.getElementById("timeline-container");
  const width = container.clientWidth;
  const margin = { top: 30, right: 40, bottom: 35, left: 40 };
  const innerW = width - margin.left - margin.right;
  const innerH = container.clientHeight - margin.top - margin.bottom;

  // Place state update markers along the bottom of the timeline
  // Use the current timeline end to position them proportionally
  const markerY = innerH + margin.top - 5;
  const isViolation = msg.violations && msg.violations.length > 0;

  // Position marker at its actual normalized time on the timeline
  const executedNodes = graphData ? graphData.nodes.filter(n => n.executionTime != null) : [];
  let x;
  if (msg.time != null && executedNodes.length > 0) {
    const tMin = d3.min(executedNodes, d => d.executionTime);
    const tMax = d3.max(executedNodes, d => d.executionTime);
    const pad = (tMax - tMin) * 0.05 || 1;
    const xScale = d3.scaleLinear().domain([tMin - pad, tMax + pad]).range([0, innerW]);
    x = margin.left + xScale(msg.time);
  } else {
    // Fallback: spread across the timeline
    x = margin.left + (innerW * (idx + 1)) / (idx + 2);
  }

  const label = Object.entries(msg.update).map(([k, v]) => `${k}=${v}`).join(", ");

  const g = svg.append("g")
    .attr("class", "state-update-marker")
    .attr("transform", `translate(${x},${markerY})`);

  // Diamond marker
  const size = 6;
  g.append("path")
    .attr("d", `M0,${-size} L${size},0 L0,${size} L${-size},0 Z`)
    .attr("fill", isViolation ? COLORS.violation : "#4caf50")
    .attr("stroke", "#fff")
    .attr("stroke-width", 1);

  // Small label
  g.append("text")
    .attr("dy", -10)
    .attr("text-anchor", "middle")
    .attr("fill", isViolation ? COLORS.violation : "#aaa")
    .attr("font-size", "8px")
    .text(label.length > 20 ? label.slice(0, 18) + ".." : label);

  // Tooltip
  g.on("mouseover", (evt) => {
    let html = `<span class="label">State Update</span><br>`;
    for (const [k, v] of Object.entries(msg.update)) {
      html += `${k} = ${v}<br>`;
    }
    if (isViolation) {
      html += `<br><span class="violation-text">VIOLATIONS:</span><br>`;
      for (const v of msg.violations) {
        html += `<span class="violation-text">${v.variable}: expected ${v.expected}, observed ${v.observed}</span><br>`;
      }
    }
    showTooltip(evt, html);
  })
  .on("mousemove", (evt) => {
    tooltip.style.left = (evt.clientX + 12) + "px";
    tooltip.style.top  = (evt.clientY + 12) + "px";
  })
  .on("mouseout", hideTooltip);

  // Pulse animation for violations
  if (isViolation) {
    g.select("path")
      .transition().duration(200)
      .attr("d", `M0,${-size*2} L${size*2},0 L0,${size*2} L${-size*2},0 Z`)
      .transition().duration(400)
      .attr("d", `M0,${-size} L${size},0 L0,${size} L${-size},0 Z`);
  }
}

function highlightViolatedEdge(causalLinkLabel) {
  if (!causalLinkLabel) return;
  violatedLinks.add(causalLinkLabel);

  const graphSvg = d3.select("#graph-svg");

  // Find edges whose causalLink label matches the violated one
  graphSvg.selectAll(".edge")
    .filter(d => d.causalLink === causalLinkLabel)
    .each(function() {
      const edgeGroup = d3.select(this);

      // Add the violated class for persistent styling
      edgeGroup.classed("violated", true);

      // Flash the edge path red then settle
      edgeGroup.selectAll(".edge-path")
        .transition().duration(200)
        .attr("stroke", COLORS.violation)
        .attr("stroke-width", 5)
        .transition().duration(800)
        .attr("stroke", COLORS.violation)
        .attr("stroke-width", 3);
    });
}

// ── MDF show/hide ──────────────────────────────────────────────────────────────
document.getElementById("btn-show-mdf").addEventListener("click", () => {
  const section = document.getElementById("mdf-section");
  section.style.display = "";
  // Auto-load if not already loaded
  if (!mdfData) document.getElementById("btn-load-mdf").click();
});

document.getElementById("btn-hide-mdf").addEventListener("click", () => {
  document.getElementById("mdf-section").style.display = "none";
});

// ── Minimum Dispatchable Form (MDF) Rendering ─────────────────────────────────
let mdfData = null;
let mdfZoom = null;

document.getElementById("btn-load-mdf").addEventListener("click", async () => {
  const statusEl = document.getElementById("mdf-status");
  statusEl.textContent = "Loading...";
  try {
    const resp = await fetch("/dispatchable-form");
    if (!resp.ok) {
      const err = await resp.json();
      throw new Error(err.detail || err.error || resp.statusText);
    }
    mdfData = await resp.json();
    if (mdfData.error) {
      statusEl.textContent = mdfData.error;
      return;
    }
    statusEl.textContent = `${mdfData.nodes.length} nodes, ${mdfData.edges.length} edges, ${mdfData.links.length} links`;
    renderMDF(mdfData);
  } catch (err) {
    statusEl.textContent = `Error: ${err.message}`;
    console.error(err);
  }
});

function renderMDF(data) {
  const container = document.getElementById("mdf-container");
  const svg = d3.select("#mdf-svg");
  svg.selectAll("*").remove();

  const width = container.clientWidth;
  const height = container.clientHeight;
  svg.attr("width", width).attr("height", height);

  // Arrow markers for MDF edge types
  const defs = svg.append("defs");
  [
    { id: "mdf-arrow-ordinary", color: "#777" },
    { id: "mdf-arrow-wait",     color: "#ff9800" },
    { id: "mdf-arrow-lc",       color: "#4caf50" },
    { id: "mdf-arrow-uc",       color: "#f44336" },
  ].forEach(({ id, color }) => {
    defs.append("marker")
      .attr("id", id)
      .attr("viewBox", "0 -5 10 10")
      .attr("refX", 18)
      .attr("refY", 0)
      .attr("markerWidth", 6)
      .attr("markerHeight", 6)
      .attr("orient", "auto")
      .append("path")
      .attr("d", "M0,-5L10,0L0,5")
      .attr("fill", color);
  });

  const g = svg.append("g");

  mdfZoom = d3.zoom()
    .scaleExtent([0.1, 5])
    .on("zoom", (evt) => g.attr("transform", evt.transform));
  svg.call(mdfZoom);

  // Force-directed layout for the MDF graph
  const nodeMap = new Map(data.nodes.map(n => [n.id, { ...n }]));
  const simNodes = data.nodes.map(n => ({ id: n.id, type: n.type }));

  // Combine edges for force simulation (use all edges)
  const simLinks = data.edges
    .filter(e => nodeMap.has(e.source) && nodeMap.has(e.target) && e.source !== e.target)
    .map(e => ({
      source: e.source,
      target: e.target,
      weight: e.weight,
      type: e.type,
      waitOnContingent: e.waitOnContingent,
    }));

  const simulation = d3.forceSimulation(simNodes)
    .force("link", d3.forceLink(simLinks).id(d => d.id).distance(120))
    .force("charge", d3.forceManyBody().strength(-300))
    .force("center", d3.forceCenter(width / 2, height / 2))
    .force("collision", d3.forceCollide(30))
    .stop();

  // Run simulation synchronously
  for (let i = 0; i < 300; i++) simulation.tick();

  // Edge type to CSS class and arrow marker
  const edgeStyle = {
    ORDINARY: { cls: "ordinary", marker: "url(#mdf-arrow-ordinary)" },
    WAIT:     { cls: "wait",     marker: "url(#mdf-arrow-wait)" },
    LC:       { cls: "lc",       marker: "url(#mdf-arrow-lc)" },
    UC:       { cls: "uc",       marker: "url(#mdf-arrow-uc)" },
  };

  // Render edges
  const edgeGroups = g.selectAll(".mdf-edge")
    .data(simLinks)
    .enter()
    .append("g")
    .attr("class", d => `mdf-edge ${(edgeStyle[d.type] || edgeStyle.ORDINARY).cls}`);

  edgeGroups.append("line")
    .attr("x1", d => d.source.x)
    .attr("y1", d => d.source.y)
    .attr("x2", d => d.target.x)
    .attr("y2", d => d.target.y)
    .attr("marker-end", d => (edgeStyle[d.type] || edgeStyle.ORDINARY).marker);

  // Invisible wide hit area
  edgeGroups.append("line")
    .attr("x1", d => d.source.x)
    .attr("y1", d => d.source.y)
    .attr("x2", d => d.target.x)
    .attr("y2", d => d.target.y)
    .attr("stroke", "transparent")
    .attr("stroke-width", 12);

  // Edge weight labels
  edgeGroups.append("text")
    .attr("class", "mdf-edge-label")
    .attr("text-anchor", "middle")
    .attr("x", d => (d.source.x + d.target.x) / 2)
    .attr("y", d => (d.source.y + d.target.y) / 2 - 5)
    .text(d => {
      let label = !isFinite(d.weight) || d.weight > 1e15 ? "∞" : `${d.weight}`;
      if (d.type === "WAIT") label += ` (wait: ${d.waitOnContingent || "?"})`;
      return label;
    });

  // Edge tooltips
  edgeGroups.on("mouseover", (evt, d) => {
    let html = `<span class="label">${d.type}</span><br>` +
      `${d.source.id} → ${d.target.id}<br>` +
      `Weight: ${d.weight}`;
    if (d.waitOnContingent) html += `<br>Wait on: ${d.waitOnContingent}`;
    showTooltip(evt, html);
  })
  .on("mousemove", (evt) => {
    tooltip.style.left = (evt.clientX + 12) + "px";
    tooltip.style.top  = (evt.clientY + 12) + "px";
  })
  .on("mouseout", hideTooltip);

  // Render nodes
  const nodeGroups = g.selectAll(".mdf-node")
    .data(simNodes)
    .enter()
    .append("g")
    .attr("class", "mdf-node")
    .attr("transform", d => `translate(${d.x},${d.y})`);

  nodeGroups.append("circle")
    .attr("r", 10)
    .attr("fill", d => d.type === "contingent" ? "#e94560" : "#2196f3")
    .attr("stroke", "#fff");

  nodeGroups.append("text")
    .attr("text-anchor", "middle")
    .attr("dy", -16)
    .text(d => d.id.length > 22 ? d.id.slice(0, 20) + ".." : d.id);

  // Node tooltips
  nodeGroups.on("mouseover", (evt, d) => {
    showTooltip(evt, `<span class="label">${d.id}</span><br>Type: ${d.type}`);
  })
  .on("mousemove", (evt) => {
    tooltip.style.left = (evt.clientX + 12) + "px";
    tooltip.style.top  = (evt.clientY + 12) + "px";
  })
  .on("mouseout", hideTooltip);

  // Fit to viewport
  const margin = 40;
  const xExt = d3.extent(simNodes, d => d.x);
  const yExt = d3.extent(simNodes, d => d.y);
  const dagW = (xExt[1] - xExt[0]) || 1;
  const dagH = (yExt[1] - yExt[0]) || 1;
  const innerW = width - margin * 2;
  const innerH = height - margin * 2;
  const scale = Math.min(innerW / dagW, innerH / dagH, 2.0);
  const offsetX = margin + (innerW - dagW * scale) / 2 - xExt[0] * scale;
  const offsetY = margin + (innerH - dagH * scale) / 2 - yExt[0] * scale;
  const initTransform = d3.zoomIdentity.translate(offsetX, offsetY).scale(scale);
  svg.call(mdfZoom.transform, initTransform);

  // Toggle controls
  function applyFilters() {
    const showWait = document.getElementById("toggle-wait").checked;
    const showContingent = document.getElementById("toggle-contingent").checked;
    const showOrdinary = document.getElementById("toggle-ordinary").checked;
    edgeGroups.style("display", d => {
      if (d.type === "WAIT" && !showWait) return "none";
      if ((d.type === "LC" || d.type === "UC") && !showContingent) return "none";
      if (d.type === "ORDINARY" && !showOrdinary) return "none";
      return null;
    });
  }
  document.getElementById("toggle-wait").onchange = applyFilters;
  document.getElementById("toggle-contingent").onchange = applyFilters;
  document.getElementById("toggle-ordinary").onchange = applyFilters;

  document.getElementById("btn-reset-mdf-zoom").onclick = () => {
    svg.transition().duration(500).call(mdfZoom.transform, initTransform);
  };
}

// Start the live WebSocket connection on page load
connectLiveWs();

// ── Window resize ──────────────────────────────────────────────────────────────
window.addEventListener("resize", () => {
  if (graphData) {
    renderTimeline(graphData);
    renderGraph(graphData);
  }
  if (mdfData) {
    renderMDF(mdfData);
  }
});
