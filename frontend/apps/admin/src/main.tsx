import "@mes/ui/src/fonts";
import "@fontsource/big-shoulders-display/500";
import "@fontsource/big-shoulders-display/600";
import "@fontsource/big-shoulders-display/700";
import "@fontsource/big-shoulders-display/800";
import "@fontsource/big-shoulders-display/900";
import "@mantine/core/styles.css";
import "@mantine/notifications/styles.css";
import "@mantine/charts/styles.css";
import "vis-timeline/styles/vis-timeline-graph2d.min.css";
import "@mes/ui/src/tokens.css";
import "./styles.css";

import { Button, MantineProvider, Stack, Text } from "@mantine/core";
import { Notifications, notifications } from "@mantine/notifications";
import { MutationCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode, Suspense, lazy } from "react";
import { createRoot } from "react-dom/client";
import { AuthProvider } from "react-oidc-context";
import { Navigate, RouterProvider, createBrowserRouter, useRouteError } from "react-router";
import { ApiError } from "@mes/api-client";
import { RequireAuth, oidcConfig } from "./auth";
import { Layout } from "./components/Layout";
import { PlantProvider } from "./components/plant";
import { HomePage } from "./pages/HomePage";
import { CategoryPage } from "./pages/CategoryPage";
import { AuditPage } from "./pages/AuditPage";
import { IntegrationPage } from "./pages/IntegrationPage";
import { LineStatusPage } from "./pages/LineStatusPage";
import { MaintenancePage } from "./pages/MaintenancePage";
import { MaterialsPage } from "./pages/MaterialsPage";
import { QualityPage } from "./pages/QualityPage";
import { TicketsPage } from "./pages/TicketsPage";
import { AiSupportPage } from "./pages/AiSupportPage";
import { NotificationsPage } from "./pages/NotificationsPage";
import { SupportAnalyticsPage } from "./pages/SupportAnalyticsPage";
import { SupportTeamPage } from "./pages/SupportTeamPage";
import { TraceabilityPage } from "./pages/TraceabilityPage";
import { TerminalsPage } from "./pages/TerminalsPage";
import { SapSimulatorPage } from "./pages/SapSimulatorPage";
import { WorkOrderDetailPage } from "./pages/WorkOrderDetailPage";
import { WorkOrdersPage } from "./pages/WorkOrdersPage";
import { theme } from "./theme";

// vis-timeline is heavy; load the board only when it is opened
const ScheduleBoardPage = lazy(() => import("./pages/ScheduleBoardPage").then((m) => ({ default: m.ScheduleBoardPage })));

// A tab left open across a redeploy holds JS that references chunk files the new build no longer
// serves (different content hash), so a lazy import 404s. Vite fires this event for exactly that
// case: reload once to pick up the new build instead of showing a crash screen. Guarded with
// sessionStorage so a genuinely broken deployment can't reload-loop the tab forever; the guard
// clears itself once the app has been up for a few seconds, so a *later* redeploy can still recover.
const RELOAD_GUARD_KEY = "mes-reload-after-preload-error";
window.addEventListener("vite:preloadError", () => {
  if (sessionStorage.getItem(RELOAD_GUARD_KEY)) return;
  sessionStorage.setItem(RELOAD_GUARD_KEY, "1");
  window.location.reload();
});
setTimeout(() => sessionStorage.removeItem(RELOAD_GUARD_KEY), 5_000);

function RouteError() {
  const error = useRouteError();
  const message = error instanceof Error ? error.message : "Something went wrong loading this page.";
  return (
    <Stack align="center" justify="center" gap="sm" style={{ minHeight: "60vh", textAlign: "center" }} p="xl">
      <Text className="page-title" fz="1.6rem">Something went <em>wrong</em></Text>
      <Text c="dimmed" size="sm" maw={480}>{message}</Text>
      <Button mt="sm" onClick={() => window.location.reload()}>Reload</Button>
    </Stack>
  );
}

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 5_000, retry: (n, e) => !(e instanceof ApiError && e.status < 500) && n < 2 },
  },
  mutationCache: new MutationCache({
    onError: (e, _v, _c, mutation) => {
      if (mutation.meta?.silent) return;
      notifications.show({
        color: "red",
        title: e instanceof ApiError ? e.code.replaceAll("_", " ") : "Error",
        message: e.message,
        autoClose: 7000,
      });
    },
  }),
});

const router = createBrowserRouter([
  {
    element: <Layout />,
    errorElement: <RouteError />,
    children: [
      { index: true, element: <HomePage /> },
      { path: "*", element: <Navigate to="/" replace /> },
      { path: "production", element: <CategoryPage group="Production" /> },
      { path: "quality-trace", element: <CategoryPage group="Quality & Trace" /> },
      { path: "support", element: <CategoryPage group="Support" /> },
      { path: "admin", element: <CategoryPage group="Admin" /> },
      { path: "work-orders", element: <WorkOrdersPage /> },
      { path: "work-orders/:id", element: <WorkOrderDetailPage /> },
      { path: "schedule", element: <Suspense fallback={null}><ScheduleBoardPage /></Suspense> },
      { path: "line-status", element: <LineStatusPage /> },
      { path: "materials", element: <MaterialsPage /> },
      { path: "quality", element: <QualityPage /> },
      { path: "traceability", element: <TraceabilityPage /> },
      { path: "maintenance", element: <MaintenancePage /> },
      { path: "tickets", element: <TicketsPage /> },
      { path: "ai-support", element: <AiSupportPage /> },
      { path: "support-analytics", element: <SupportAnalyticsPage /> },
      { path: "notifications", element: <NotificationsPage /> },
      { path: "support-team", element: <SupportTeamPage /> },
      { path: "terminals", element: <TerminalsPage /> },
      { path: "integration", element: <IntegrationPage /> },
      { path: "audit", element: <AuditPage /> },
      { path: "sap-simulator", element: <SapSimulatorPage /> },
    ],
  },
]);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <MantineProvider theme={theme} forceColorScheme="light">
      <Notifications position="bottom-right" />
      <AuthProvider {...oidcConfig}>
        <QueryClientProvider client={queryClient}>
          <RequireAuth>
            <PlantProvider>
              <RouterProvider router={router} />
            </PlantProvider>
          </RequireAuth>
        </QueryClientProvider>
      </AuthProvider>
    </MantineProvider>
  </StrictMode>,
);
