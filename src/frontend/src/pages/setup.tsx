import { useState } from 'react';
import { Link as RouterLink, useNavigate } from 'react-router-dom';
import {
  Alert,
  Button,
  ButtonGroup,
  FormControl,
  FormControlLabel,
  FormLabel,
  MenuItem,
  Select,
  Stack,
  styled,
  TextField,
  ToggleButton,
  ToggleButtonGroup,
  Typography,
} from '@mui/material';
import { MainLayout } from '../components/common';
import { ModelSelect, NumberInput } from '../components/inputs';
import { useCluster } from '../services';
import { useRefCallback } from '../hooks';
import { Box } from '@mui/material';

// Styled component for the glowing title with light flow effect
const GlowingTitle = styled(Box)({
  position: 'relative',
  display: 'inline-block',
  '& .title-text': {
    fontSize: '2.2rem',
    fontWeight: 700,
    letterSpacing: '0.5px',
    background: 'linear-gradient(135deg, #00F0FF 0%, #B026FF 50%, #00F0FF 100%)',
    backgroundSize: '200% auto',
    WebkitBackgroundClip: 'text',
    WebkitTextFillColor: 'transparent',
    backgroundClip: 'text',
    animation: 'lightFlow 3s linear infinite',
    textShadow: '0 0 30px rgba(0, 240, 255, 0.3)',
  },
  '@keyframes lightFlow': {
    '0%': {
      backgroundPosition: '0% center',
    },
    '100%': {
      backgroundPosition: '200% center',
    },
  },
});

const Subtitle = styled(Typography)(({ theme }) => ({
  fontSize: '0.9rem',
  color: 'rgba(255, 255, 255, 0.75)',
  fontWeight: 400,
  maxWidth: '100%',
  lineHeight: 1.5,
  marginTop: theme.spacing(0.5),
  '& .highlight': {
    color: '#00F0FF',
    fontWeight: 600,
  },
}));

export default function PageSetup() {
  const [
    {
      config: { networkType, initNodesNumber, modelInfo, modelName: selectedModelName },
      clusterInfo: {
        status: clusterStatus,
        initNodesNumber: clusterInitNodesNumber,
        modelName: clusterModelName,
      },
    },
    {
      config: { setNetworkType, setInitNodesNumber },
      init,
    },
  ] = useCluster();

  const navigate = useNavigate();

  const [loading, setLoading] = useState(false);

  const onContinue = useRefCallback(async () => {
    const shouldInit =
      clusterStatus === 'idle'
      || clusterStatus === 'failed'
      || clusterInitNodesNumber !== initNodesNumber
      || clusterModelName !== selectedModelName;

    if (shouldInit) {
      setLoading(true);
      Promise.resolve()
        .then(() => init())
        .then(() => navigate('/join'))
        .catch((e) => console.error(e))
        .finally(() => setLoading(false));
      return;
    } else {
      navigate('/join');
    }
  });

  return (
    <MainLayout contentMaxWidth="48rem">
      <Stack gap={3} width="100%">
        <Stack gap={0.75} alignItems='center' sx={{ textAlign: 'center' }}>
          <GlowingTitle>
            <span className="title-text">Unleash the Power of Decentralized Intelligence</span>
          </GlowingTitle>
          <Subtitle>
            Prakasa (Sanskrit: प्रकाश) — The decentralized <span className="highlight">"Light"</span> of intelligence.
            A privacy-preserving P2P GPU inference network powered by Nostr protocol, transforming idle GPU resources into a unified, resilient intelligence layer.
          </Subtitle>
        </Stack>

        <Stack gap={1.5} sx={{ width: '31rem', alignSelf: 'center', maxWidth: '100%' }}>
          <Stack gap={0.5}>
            <Typography variant='body1'>Step 1 - Configure your decentralized compute network</Typography>
            <Typography variant='body2' color='text.secondary' fontWeight='regular'>
              Prakasa distributes model inference across heterogeneous GPU nodes in a P2P network. 
              Specify the number of nodes you'd like to contribute and their connection type.
            </Typography>
          </Stack>

          <Stack direction='row' justifyContent='space-between' alignItems='center' gap={2}>
            <Typography color='text.secondary'>Node Number</Typography>
            <NumberInput
              sx={{ width: '10rem', boxShadow: 'none', bgcolor: 'transparent' }}
              slotProps={{
                root: {
                  sx: {
                    bgcolor: 'transparent',
                    '&:hover': { bgcolor: 'transparent' },
                    '&:focus-within': { bgcolor: 'transparent' },
                  },
                },
                input: {
                  sx: {
                    bgcolor: 'transparent !important',
                    '&:focus': { outline: 'none' },
                  },
                },
              }}
              value={initNodesNumber}
              onChange={(e) => setInitNodesNumber(Number(e.target.value))}
            />
          </Stack>

          <Stack direction='row' justifyContent='space-between' alignItems='center' gap={2}>
            <Typography color='text.secondary'>
              Are you nodes within the same local network?
            </Typography>
            <ToggleButtonGroup
              sx={{ width: '10rem', textTransform: 'none' }}
              exclusive
              value={networkType}
              onChange={(_, value) => value && setNetworkType(value)}
            >
              <ToggleButton value='local' sx={{ textTransform: 'none' }}>
                Local
              </ToggleButton>
              <ToggleButton value='remote' sx={{ textTransform: 'none' }}>
                Remote
              </ToggleButton>
            </ToggleButtonGroup>
          </Stack>
        </Stack>

        <Stack gap={1.5} sx={{ width: '31rem', alignSelf: 'center', maxWidth: '100%' }}>
          <Stack gap={0.5}>
            <Typography variant='body1'>Step 2 - Select your AI model</Typography>
            <Typography variant='body2' color='text.secondary' fontWeight='regular'>
              Choose from state-of-the-art open-source models. Larger models require more collective GPU resources.
              New to Prakasa? Start with smaller models for a smoother experience.
            </Typography>
          </Stack>

          <ModelSelect />

          {!!modelInfo && modelInfo.vram > 0 && (
            <Alert key='vram-warning' severity='warning' variant='standard'>
              <Typography variant='inherit'>
                {[
                  `You’ll need a `,
                  <strong>{`minimum of ${modelInfo.vram} GB of total VRAM`}</strong>,
                  ` to host this model.`,
                ]}
              </Typography>
            </Alert>
          )}
        </Stack>

        <Stack direction='row' justifyContent='flex-end' alignItems='center' gap={2} sx={{ width: '31rem', alignSelf: 'center', maxWidth: '100%' }}>
          <Button loading={loading} onClick={onContinue}>
            Continue
          </Button>
        </Stack>
      </Stack>
    </MainLayout>
  );
}
