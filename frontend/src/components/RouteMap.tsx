"use client";

import { useEffect, useRef, useState } from "react";
import { cx } from "@/components/ui";
import type { PlanStop } from "@/lib/api";

/**
 * A day's route on a Singapore map.
 *
 * Google Maps when it loads, with the real road-following path from the Directions API. A
 * schematic Singapore falls in behind it when the Maps JavaScript API is unreachable -- the same
 * real coordinates, plotted on a projection of the island's bounding box, so the demo degrades to
 * something readable rather than an empty grey box.
 *
 * Coordinates are whatever was stored on the order. Nothing is nudged to make the picture tidier:
 * a moved pin would be a prettier map of a route that does not exist.
 */

const SG_BOUNDS = { latMin: 1.21, latMax: 1.48, lngMin: 103.6, lngMax: 104.05 };

export interface MapPoint {
  lat: number;
  lng: number;
  label: string;
  name: string;
  detail?: string;
  highlighted?: boolean;
  approximate?: boolean;
}

export function stopsToPoints(stops: PlanStop[], highlightJobId?: string | null): MapPoint[] {
  return stops
    .filter((s): s is PlanStop & { lat: number; lng: number } => s.lat !== null && s.lng !== null)
    .map((s) => ({
      lat: s.lat,
      lng: s.lng,
      label: String(s.sequence_index),
      name: s.customer_name,
      detail: `${s.arrival}–${s.departure}${s.address ? ` · ${s.address}` : ""}`,
      highlighted: s.job_id === highlightJobId,
      approximate: !s.precise_location,
    }));
}

export function RouteMap({
  points,
  depot,
  apiKey,
  className,
}: {
  points: MapPoint[];
  depot: { lat: number; lng: number; address: string } | null;
  apiKey?: string;
  className?: string;
}) {
  const [status, setStatus] = useState<"loading" | "ready" | "unavailable">(
    apiKey ? "loading" : "unavailable",
  );

  return (
    <div className={cx("relative overflow-hidden rounded-[12px] border border-rail bg-sunk", className)}>
      {status !== "unavailable" && apiKey && (
        <GoogleRouteMap
          points={points}
          depot={depot}
          apiKey={apiKey}
          onReady={() => setStatus("ready")}
          onFail={() => setStatus("unavailable")}
        />
      )}
      {status !== "ready" && (
        <SchematicMap points={points} depot={depot} loading={status === "loading"} />
      )}
    </div>
  );
}

// -- Google -------------------------------------------------------------------

declare global {
  interface Window {
    google?: any;
    __dispatchMapReady?: () => void;
  }
}

let loaderPromise: Promise<void> | null = null;

/** One script tag per page, however many maps mount. */
function loadMaps(apiKey: string): Promise<void> {
  if (window.google?.maps) return Promise.resolve();
  if (loaderPromise) return loaderPromise;

  loaderPromise = new Promise<void>((resolve, reject) => {
    const script = document.createElement("script");
    script.src = `https://maps.googleapis.com/maps/api/js?key=${apiKey}&callback=__dispatchMapReady&loading=async`;
    script.async = true;
    script.onerror = () => reject(new Error("maps script failed to load"));
    window.__dispatchMapReady = () => resolve();
    // An invalid key still loads the script but never calls back, so bound the wait rather than
    // leaving the panel in a permanent loading state.
    setTimeout(() => reject(new Error("maps did not initialise")), 8000);
    document.head.appendChild(script);
  });
  return loaderPromise;
}

function GoogleRouteMap({
  points,
  depot,
  apiKey,
  onReady,
  onFail,
}: {
  points: MapPoint[];
  depot: { lat: number; lng: number; address: string } | null;
  apiKey: string;
  onReady: () => void;
  onFail: () => void;
}) {
  const holder = useRef<HTMLDivElement>(null);
  const mapRef = useRef<any>(null);
  const drawn = useRef<any[]>([]);

  useEffect(() => {
    let cancelled = false;

    loadMaps(apiKey)
      .then(() => {
        if (cancelled || !holder.current) return;
        mapRef.current ??= new window.google.maps.Map(holder.current, {
          center: depot ?? { lat: 1.3521, lng: 103.8198 },
          zoom: 11,
          disableDefaultUI: true,
          zoomControl: true,
          // A quiet base map: the route is the subject, not the retail landscape.
          styles: [
            { featureType: "poi", stylers: [{ visibility: "off" }] },
            { featureType: "transit", stylers: [{ visibility: "off" }] },
            { featureType: "road", elementType: "labels", stylers: [{ visibility: "simplified" }] },
          ],
        });
        onReady();
        draw();
      })
      .catch(() => !cancelled && onFail());

    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiKey]);

  useEffect(() => {
    if (mapRef.current) draw();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [points, depot]);

  function draw() {
    const google = window.google;
    const map = mapRef.current;
    if (!google || !map) return;

    drawn.current.forEach((o) => o.setMap?.(null));
    drawn.current = [];

    const bounds = new google.maps.LatLngBounds();

    if (depot) {
      drawn.current.push(
        new google.maps.Marker({
          map,
          position: depot,
          title: `Depot — ${depot.address}`,
          icon: {
            path: google.maps.SymbolPath.CIRCLE,
            scale: 7,
            fillColor: "#2f5d7c",
            fillOpacity: 1,
            strokeColor: "#ffffff",
            strokeWeight: 2,
          },
          zIndex: 5,
        }),
      );
      bounds.extend(depot);
    }

    points.forEach((p) => {
      drawn.current.push(
        new google.maps.Marker({
          map,
          position: { lat: p.lat, lng: p.lng },
          label: { text: p.label, color: "#ffffff", fontSize: "11px", fontWeight: "600" },
          title: `${p.label}. ${p.name}${p.detail ? ` — ${p.detail}` : ""}`,
          icon: {
            path: google.maps.SymbolPath.CIRCLE,
            scale: p.highlighted ? 13 : 10,
            fillColor: p.highlighted ? "#2a6a4f" : "#3f4a54",
            fillOpacity: 1,
            strokeColor: "#ffffff",
            strokeWeight: p.highlighted ? 3 : 2,
          },
          zIndex: p.highlighted ? 20 : 10,
        }),
      );
      bounds.extend({ lat: p.lat, lng: p.lng });
    });

    if (points.length && depot) {
      const path = [depot, ...points.map((p) => ({ lat: p.lat, lng: p.lng })), depot];
      // Ask for the real driving path; a straight line is the honest fallback if it fails.
      new google.maps.DirectionsService()
        .route({
          origin: depot,
          destination: depot,
          waypoints: points.map((p) => ({ location: { lat: p.lat, lng: p.lng }, stopover: true })),
          travelMode: google.maps.TravelMode.DRIVING,
        })
        .then((result: any) => {
          drawn.current.push(
            new google.maps.Polyline({
              map,
              path: result.routes[0].overview_path,
              strokeColor: "#2f5d7c",
              strokeOpacity: 0.85,
              strokeWeight: 3,
            }),
          );
        })
        .catch(() => {
          drawn.current.push(
            new google.maps.Polyline({
              map,
              path,
              strokeColor: "#93a0ab",
              strokeOpacity: 0,
              strokeWeight: 2,
              icons: [
                { icon: { path: "M 0,-1 0,1", strokeOpacity: 0.8, scale: 3 }, offset: "0", repeat: "12px" },
              ],
            }),
          );
        });
    }

    if (!bounds.isEmpty()) map.fitBounds(bounds, 56);
  }

  return <div ref={holder} className="absolute inset-0" aria-label="Route map" />;
}

// -- fallback -----------------------------------------------------------------

/**
 * Singapore, schematically. Not a substitute for a real map, but it plots the same real
 * coordinates in the right relative positions, which is enough to see a route's shape and where a
 * new stop landed.
 */
function SchematicMap({
  points,
  depot,
  loading,
}: {
  points: MapPoint[];
  depot: { lat: number; lng: number } | null;
  loading: boolean;
}) {
  const W = 800;
  const H = 460;
  const project = (lat: number, lng: number) => ({
    x: ((lng - SG_BOUNDS.lngMin) / (SG_BOUNDS.lngMax - SG_BOUNDS.lngMin)) * W,
    y: H - ((lat - SG_BOUNDS.latMin) / (SG_BOUNDS.latMax - SG_BOUNDS.latMin)) * H,
  });

  const projected = points.map((p) => ({ ...p, ...project(p.lat, p.lng) }));
  const depotXY = depot ? project(depot.lat, depot.lng) : null;
  const path = depotXY
    ? [depotXY, ...projected.map((p) => ({ x: p.x, y: p.y })), depotXY]
    : projected.map((p) => ({ x: p.x, y: p.y }));

  return (
    <div className="absolute inset-0 flex flex-col">
      <svg viewBox={`0 0 ${W} ${H}`} className="h-full w-full" role="img" aria-label="Route map (schematic)">
        <rect width={W} height={H} fill="var(--color-sunk)" />
        {/* A rough island outline: enough to orient, not a claim to accuracy. */}
        <path
          d="M120,236 C150,196 208,172 268,166 C320,160 366,172 414,164 C470,154 520,150 570,164
             C622,178 662,200 682,232 C696,256 690,282 664,300 C620,330 556,344 490,346
             C420,348 344,342 276,326 C208,310 150,286 122,258 Z"
          fill="var(--color-surface)"
          stroke="var(--color-rail-strong)"
          strokeWidth="1.5"
        />
        {path.length > 1 && (
          <polyline
            points={path.map((p) => `${p.x},${p.y}`).join(" ")}
            fill="none"
            stroke="var(--color-accent)"
            strokeOpacity="0.5"
            strokeWidth="2"
            strokeDasharray="5 4"
          />
        )}
        {depotXY && (
          <circle cx={depotXY.x} cy={depotXY.y} r="7" fill="var(--color-accent)" stroke="#fff" strokeWidth="2" />
        )}
        {projected.map((p, i) => (
          <g key={i}>
            <circle
              cx={p.x}
              cy={p.y}
              r={p.highlighted ? 13 : 11}
              fill={p.highlighted ? "var(--color-locked)" : "var(--color-ink-soft)"}
              stroke="#fff"
              strokeWidth={p.highlighted ? 3 : 2}
            />
            <text
              x={p.x}
              y={p.y + 4}
              textAnchor="middle"
              fill="#fff"
              fontSize="11"
              fontWeight="600"
              fontFamily="var(--font-mono)"
            >
              {p.label}
            </text>
            <title>{`${p.label}. ${p.name}${p.detail ? ` — ${p.detail}` : ""}`}</title>
          </g>
        ))}
      </svg>
      <p className="absolute bottom-2 left-3 font-mono text-[10px] uppercase tracking-[0.1em] text-ink-faint">
        {loading ? "Loading map…" : "Schematic — Google Maps unavailable"}
      </p>
    </div>
  );
}
