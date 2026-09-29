import { useQuery } from "@tanstack/react-query";
import { type ReactNode, createContext, useContext, useEffect, useState } from "react";
import { core, type Plant, unwrap } from "@mes/api-client";

type PlantCtx = { plants: Plant[]; plant: Plant | null; setPlantId: (id: string) => void };
const Ctx = createContext<PlantCtx>({ plants: [], plant: null, setPlantId: () => {} });

const KEY = "mes.admin.plant";

function stored(): string | null {
  try {
    return localStorage.getItem(KEY);
  } catch {
    return null;
  }
}

export function PlantProvider({ children }: { children: ReactNode }) {
  const { data: plants = [] } = useQuery({ queryKey: ["plants"], queryFn: () => unwrap(core.GET("/api/v1/plants")) });
  const [plantId, setPlantIdState] = useState<string | null>(stored());
  const plant = plants.find((p) => p.id === plantId) ?? plants[0] ?? null;

  useEffect(() => {
    if (plant && plant.id !== plantId) setPlantIdState(plant.id);
  }, [plant, plantId]);

  const setPlantId = (id: string) => {
    setPlantIdState(id);
    try {
      localStorage.setItem(KEY, id);
    } catch {
      /* storage unavailable: selection just isn't remembered */
    }
  };
  return <Ctx.Provider value={{ plants, plant, setPlantId }}>{children}</Ctx.Provider>;
}

export const usePlant = () => useContext(Ctx);
