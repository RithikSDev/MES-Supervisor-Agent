import { Button, Center, Loader, Stack, Text, Title } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { WebStorageStateStore } from "oidc-client-ts";
import { type ReactNode, createContext, useContext, useEffect, useState } from "react";
import { type AuthProviderProps, useAuth } from "react-oidc-context";
import { core, type Me, setAccessToken, unwrap } from "@mes/api-client";

export const oidcConfig: AuthProviderProps = {
  authority: import.meta.env.VITE_OIDC_AUTHORITY ?? "http://localhost:8080/realms/mes",
  client_id: import.meta.env.VITE_OIDC_CLIENT_ID ?? "mes-admin",
  redirect_uri: `${window.location.origin}/`,
  post_logout_redirect_uri: `${window.location.origin}/`,
  scope: "openid profile email",
  automaticSilentRenew: true,
  userStore: new WebStorageStateStore({ store: window.sessionStorage }),
  onSigninCallback: () => window.history.replaceState({}, document.title, window.location.pathname),
};

const MeContext = createContext<Me | null>(null);

export function useMe(): Me {
  const me = useContext(MeContext);
  if (!me) throw new Error("useMe outside RequireAuth");
  return me;
}

export function useCan(permission: string): boolean {
  return useMe().permissions.includes(permission);
}

/** Gate: signs in via Keycloak, then loads /me (which also provisions the MES user). */
export function RequireAuth({ children }: { children: ReactNode }) {
  const auth = useAuth();
  const [tokenReady, setTokenReady] = useState(false);

  useEffect(() => {
    setAccessToken(auth.user?.access_token);
    setTokenReady(Boolean(auth.user?.access_token));
  }, [auth.user?.access_token]);

  const me = useQuery({
    queryKey: ["me", auth.user?.profile.sub],
    queryFn: () => unwrap(core.GET("/api/v1/me")),
    enabled: tokenReady,
  });

  if (auth.isLoading || (auth.isAuthenticated && (me.isPending || !tokenReady))) {
    return (
      <Center h="100vh">
        <Loader color="brand" type="bars" />
      </Center>
    );
  }
  if (auth.error || me.error) {
    return (
      <Center h="100vh">
        <Stack align="center" gap="xs">
          <Title order={3}>Sign-in problem</Title>
          <Text c="dimmed" size="sm">{String(auth.error?.message ?? me.error?.message)}</Text>
          <Button onClick={() => auth.signinRedirect()}>Try again</Button>
        </Stack>
      </Center>
    );
  }
  if (!auth.isAuthenticated) {
    return <SignIn onSignIn={() => auth.signinRedirect()} />;
  }
  return <MeContext.Provider value={me.data!}>{children}</MeContext.Provider>;
}

function SignIn({ onSignIn }: { onSignIn: () => void }) {
  return (
    <Center h="100vh">
      <Stack align="center" gap="md" className="panel" p={44} w={420}>
        <span className="brand-mark" style={{ width: 52, height: 52 }}>
          <svg width="30" height="30" viewBox="0 0 32 32" aria-hidden>
            <path d="M5 25V7l11 10 11-10v18" stroke="#f2a900" strokeWidth="3.2" fill="none" strokeLinejoin="round" />
          </svg>
        </span>
        <div className="display" style={{ fontSize: "2.2rem", textAlign: "center" }}>MES <em>Workspace</em></div>
        <Text size="sm" c="dimmed" ta="center">
          Sign in with your plant account. Demo users: admin1, supervisor1, operator1 (password <code>demo</code>).
        </Text>
        <Button size="sm" fullWidth onClick={onSignIn}>Sign in</Button>
      </Stack>
    </Center>
  );
}
