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

// ── Color palette ──────────────────────────────────────────────────────────────
const COLORS = {
  start:        "#4caf50",
  end:          "#f44336",
  action_start: "#2196f3",
  action_end:   "#ff9800",
  event:        "#9c27b0",
  edge:         "#555",
  causal:       "#ffd700",
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
}

// ── Graph Rendering ───────────────────────────────────────────────────────────
function renderGraph(data) {
  const container = document.getElementById("graph-container");
  const svg = d3.select("#graph-svg");
  svg.selectAll("*").remove();

  const width  = container.clientWidth;
  const height = container.clientHeight;

  svg.attr("width", width).attr("height", height);

  // Arrowhead marker
  svg.append("defs").selectAll("marker")
    .data(["arrow", "arrow-causal"])
    .enter()
    .append("marker")
    .attr("id", d => d)
    .attr("viewBox", "0 -5 10 10")
    .attr("refX", 20)
    .attr("refY", 0)
    .attr("markerWidth", 6)
    .attr("markerHeight", 6)
    .attr("orient", "auto")
    .append("path")
    .attr("d", "M0,-5L10,0L0,5")
    .attr("fill", d => d === "arrow-causal" ? COLORS.causal : "#777");

  const g = svg.append("g");

  // Zoom
  graphZoom = d3.zoom()
    .scaleExtent([0.1, 5])
    .on("zoom", (evt) => g.attr("transform", evt.transform));
  svg.call(graphZoom);

  // Force simulation
  const nodeMap = new Map(data.nodes.map(n => [n.id, { ...n }]));
  const nodes = Array.from(nodeMap.values());
  const links = data.edges.map(e => ({
    ...e,
    source: e.source,
    target: e.target,
  }));

  const simulation = d3.forceSimulation(nodes)
    .force("link", d3.forceLink(links).id(d => d.id).distance(120))
    .force("charge", d3.forceManyBody().strength(-300))
    .force("center", d3.forceCenter(width / 2, height / 2))
    .force("collision", d3.forceCollide(30));

  // Edges
  const edgeGroups = g.selectAll(".edge")
    .data(links)
    .enter()
    .append("g")
    .attr("class", d => {
      let cls = "edge";
      if (d.hasCausalLink) cls += " causal";
      if (d.isEpisode) cls += " episode";
      return cls;
    });

  const edgeLines = edgeGroups.append("line")
    .attr("stroke", d => d.hasCausalLink ? COLORS.causal : COLORS.edge)
    .attr("marker-end", d => d.hasCausalLink ? "url(#arrow-causal)" : "url(#arrow)");

  const edgeLabels = edgeGroups.append("text")
    .attr("class", "edge-label")
    .attr("text-anchor", "middle")
    .attr("dy", -6)
    .text(d => {
      let label = `[${d.lb}, ${d.ub}]`;
      if (d.causalLink) label += ` ${d.causalLink}`;
      return label;
    });

  // Edge tooltips
  edgeGroups.on("mouseover", (evt, d) => {
    let html = `<span class="label">${d.id}</span><br>` +
      `${d.source.id || d.source} &rarr; ${d.target.id || d.target}<br>` +
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

  // Nodes
  const nodeGroups = g.selectAll(".node")
    .data(nodes)
    .enter()
    .append("g")
    .attr("class", "node")
    .call(d3.drag()
      .on("start", (evt, d) => {
        if (!evt.active) simulation.alphaTarget(0.3).restart();
        d.fx = d.x;
        d.fy = d.y;
      })
      .on("drag", (evt, d) => {
        d.fx = evt.x;
        d.fy = evt.y;
      })
      .on("end", (evt, d) => {
        if (!evt.active) simulation.alphaTarget(0);
        d.fx = null;
        d.fy = null;
      })
    );

  nodeGroups.append("circle")
    .attr("r", 10)
    .attr("fill", d => nodeColor(d.type))
    .attr("stroke", "#fff");

  nodeGroups.append("text")
    .attr("dx", 14)
    .attr("dy", 4)
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

  // Tick
  simulation.on("tick", () => {
    edgeLines
      .attr("x1", d => d.source.x)
      .attr("y1", d => d.source.y)
      .attr("x2", d => d.target.x)
      .attr("y2", d => d.target.y);

    edgeLabels
      .attr("x", d => (d.source.x + d.target.x) / 2)
      .attr("y", d => (d.source.y + d.target.y) / 2);

    nodeGroups.attr("transform", d => `translate(${d.x},${d.y})`);
  });

  // Toggle causal link highlight
  document.getElementById("toggle-causal").onchange = (evt) => {
    const show = evt.target.checked;
    edgeGroups.filter(d => d.hasCausalLink)
      .classed("causal", show);
  };

  // Reset zoom
  document.getElementById("btn-reset-zoom").onclick = () => {
    svg.transition().duration(500).call(graphZoom.transform, d3.zoomIdentity);
  };
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

// Start the live WebSocket connection on page load
connectLiveWs();

// ── Window resize ──────────────────────────────────────────────────────────────
window.addEventListener("resize", () => {
  if (graphData) {
    renderTimeline(graphData);
    renderGraph(graphData);
  }
});
