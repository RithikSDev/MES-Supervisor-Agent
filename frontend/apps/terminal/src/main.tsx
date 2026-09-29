import "@mes/ui/src/fonts";
import "@mantine/core/styles.css";
import "@mantine/notifications/styles.css";
import "@mes/ui/src/tokens.css";
import "./terminal.css";

import { MantineProvider } from "@mantine/core";
import { Notifications } from "@mantine/notifications";
import { theme } from "@mes/ui";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";

const queryClient = new QueryClient({ defaultOptions: { queries: { staleTime: 2_000, retry: false } } });

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <MantineProvider theme={theme} forceColorScheme="dark">
      <Notifications position="top-center" autoClose={4000} styles={{ notification: { fontSize: 18 } }} />
      <QueryClientProvider client={queryClient}>
        <App />
      </QueryClientProvider>
    </MantineProvider>
  </StrictMode>,
);
