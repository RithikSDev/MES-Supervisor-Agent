import { ActionIcon, Badge, Button, FileInput, Group, Select, SimpleGrid, Stack, Table, Text, TextInput } from "@mantine/core";
import { agent } from "@mes/api-client";
import { IconBooks, IconSearch, IconTrash, IconUpload, IconX } from "@tabler/icons-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useMe } from "../auth";
import { fromNow } from "../lib/format";

type Doc = {
  id: string; title: string; kind: string; equipment_type: string | null; screen: string | null; source: string;
  builtin: boolean; pages: number | null; chars: number; chunks: number; uploaded_by: string; created_at: string;
};
type Hit = { doc_id: string; document: string; kind: string; equipment_type: string | null; section: string | null; page: number | null; text: string };

const KINDS = [
  { value: "MANUAL", label: "OEM manual" }, { value: "ALARM_LIST", label: "Fault-code list (CSV)" },
  { value: "MACHINE_GUIDE", label: "Machine guide" }, { value: "USER_GUIDE", label: "MES user guide" },
  { value: "NOTE", label: "Note" },
];
const TYPES = ["CNC_MILL", "CNC_LATHE", "ASSEMBLY", "TESTER", "WASHER"];
const KIND_LABEL = Object.fromEntries(KINDS.map((k) => [k.value, k.label]));

/** What the AI agents know besides the live data: machine guides, OEM manuals, fault codes, the MES user guide. */
export function KnowledgePanel() {
  const qc = useQueryClient();
  const isAdmin = useMe().roles.includes("admin");
  const docs = useQuery({ queryKey: ["agent", "knowledge"], queryFn: () => agent<Doc[]>("/knowledge") });
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [kind, setKind] = useState<string | null>("MANUAL");
  const [etype, setEtype] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [docSearch, setDocSearch] = useState("");
  const [docKind, setDocKind] = useState<string | null>(null);
  const docRows = useMemo(() => {
    const s = docSearch.trim().toLowerCase();
    return (docs.data ?? []).filter((d) => (!docKind || d.kind === docKind) && (!s
      || d.title.toLowerCase().includes(s) || d.source.toLowerCase().includes(s) || (d.equipment_type ?? "").toLowerCase().includes(s)));
  }, [docs.data, docSearch, docKind]);
  const upload = useMutation({
    mutationFn: () => {
      const form = new FormData();
      form.append("file", file!);
      form.append("title", title || file!.name);
      form.append("kind", kind ?? "MANUAL");
      if (etype) form.append("equipment_type", etype);
      return agent<{ chunks: number; pages: number | null }>("/knowledge", { method: "POST", body: form });
    },
    onSuccess: () => { setFile(null); setTitle(""); qc.invalidateQueries({ queryKey: ["agent", "knowledge"] }); },
  });
  const remove = useMutation({
    mutationFn: (id: string) => agent(`/knowledge/${id}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["agent", "knowledge"] }),
  });
  const search = useMutation({ mutationFn: () => agent<Hit[]>(`/knowledge/search?q=${encodeURIComponent(q)}`) });

  return (
    <Stack>
      <Text size="sm" c="dimmed" maw={900}>
        The AI agents search these documents for machine faults (alarm codes, causes, checks, spare parts) and for how to
        use MES. Built-in documents come from the knowledge/ folder; add OEM manuals (PDF with text), fault-code lists
        (CSV: code, text, cause, remedy, ...) and notes.
      </Text>
      {isAdmin && (
        <div className="panel" style={{ padding: 14 }}>
          <Group gap={6} mb={10}><IconUpload size={15} /><Text fw={700} size="sm">Add a document</Text></Group>
          <SimpleGrid cols={{ base: 1, md: 4 }}>
            <FileInput label="File" placeholder="PDF, CSV, TXT or MD" accept=".pdf,.csv,.txt,.md" value={file} onChange={setFile} clearable />
            <TextInput label="Title" placeholder="e.g. MX-500 maintenance manual" value={title} onChange={(e) => setTitle(e.currentTarget.value)} />
            <Select label="Kind" data={KINDS} value={kind} onChange={setKind} allowDeselect={false} />
            <Select label="Machine type" placeholder="all machines" data={TYPES} value={etype} onChange={setEtype} clearable />
          </SimpleGrid>
          <Group justify="flex-end" mt="sm">
            {upload.data && <Text size="xs" c="green.7">Added: {upload.data.chunks} sections{upload.data.pages ? `, ${upload.data.pages} pages` : ""}</Text>}
            <Button leftSection={<IconUpload size={14} />} disabled={!file} loading={upload.isPending} onClick={() => upload.mutate()}>Upload</Button>
          </Group>
        </div>
      )}
      <Group>
        <TextInput style={{ flex: 1, maxWidth: 520 }} placeholder="Try a search: alarm 2031, clamp pressure low, publish schedule…" value={q}
          onChange={(e) => setQ(e.currentTarget.value)} onKeyDown={(e) => { if (e.key === "Enter" && q.trim().length > 1) search.mutate(); }} />
        <Button variant="default" leftSection={<IconSearch size={14} />} disabled={q.trim().length < 2} loading={search.isPending} onClick={() => search.mutate()}>Search</Button>
      </Group>
      {search.data && (
        <Stack gap={6}>
          {search.data.length === 0 && <Text size="sm" c="dimmed">Nothing found.</Text>}
          {search.data.map((h, i) => (
            <div key={i} className="panel" style={{ padding: 10 }}>
              <Text size="sm" fw={600}>{h.document}{h.section ? ` · ${h.section}` : ""}{h.page ? ` · page ${h.page}` : ""}</Text>
              <Text size="xs" c="dimmed" lineClamp={3} style={{ whiteSpace: "pre-wrap" }}>{h.text}</Text>
            </div>
          ))}
        </Stack>
      )}
      <Group justify="space-between" wrap="wrap">
        <Group gap={6}><IconBooks size={15} /><Text fw={700} size="sm">Documents</Text><Text size="xs" c="dimmed">{docRows.length} of {docs.data?.length ?? 0}</Text></Group>
        <Group gap={8} wrap="wrap">
          <Select size="xs" w={170} placeholder="All kinds" clearable data={KINDS} value={docKind} onChange={setDocKind} />
          <TextInput size="xs" w={220} leftSection={<IconSearch size={14} />}
            rightSection={docSearch ? (
              <ActionIcon variant="subtle" color="gray" size="xs" onClick={() => setDocSearch("")} aria-label="Clear search"><IconX size={12} /></ActionIcon>
            ) : undefined}
            placeholder="Title, source, machine…" value={docSearch} onChange={(e) => setDocSearch(e.currentTarget.value)} />
        </Group>
      </Group>
      <Table className="panel" verticalSpacing={6}>
        <Table.Thead><Table.Tr><Table.Th>Document</Table.Th><Table.Th>Kind</Table.Th><Table.Th>Machine type</Table.Th>
          <Table.Th ta="right">Sections</Table.Th><Table.Th>Added</Table.Th><Table.Th /></Table.Tr></Table.Thead>
        <Table.Tbody>
          {docRows.map((d) => (
            <Table.Tr key={d.id}>
              <Table.Td><Text size="sm">{d.title}</Text><Text size="xs" c="dimmed" className="mono">{d.source}{d.pages ? ` · ${d.pages} pages` : ""}</Text></Table.Td>
              <Table.Td><Badge size="xs" variant="light" color={d.builtin ? "gray" : "brand"}>{KIND_LABEL[d.kind] ?? d.kind}</Badge></Table.Td>
              <Table.Td><Text size="xs">{d.equipment_type ?? "all"}</Text></Table.Td>
              <Table.Td ta="right" className="mono">{d.chunks}</Table.Td>
              <Table.Td><Text size="xs">{d.builtin ? "built in" : `${d.uploaded_by} · ${fromNow(d.created_at)}`}</Text></Table.Td>
              <Table.Td>{isAdmin && !d.builtin && (
                <Button size="compact-xs" variant="subtle" color="gray" aria-label="Remove" onClick={() => remove.mutate(d.id)}><IconTrash size={13} /></Button>
              )}</Table.Td>
            </Table.Tr>
          ))}
          {docs.data?.length !== 0 && docRows.length === 0 && (
            <Table.Tr><Table.Td colSpan={6}><Text size="sm" c="dimmed">No documents match this filter.</Text></Table.Td></Table.Tr>
          )}
        </Table.Tbody>
      </Table>
    </Stack>
  );
}
