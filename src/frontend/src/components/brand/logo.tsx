import type { FC } from 'react';
import { Box, Stack, Typography, styled } from '@mui/material';

const LogoContainer = styled(Stack)(({ theme }) => ({
  alignItems: 'center',
  gap: theme.spacing(1),
}));

const AsciiContainer = styled(Box)({
  width: '600px',
  height: '180px',
  backgroundColor: '#000000',
  borderRadius: '8px',
  overflow: 'hidden',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
});

const LogoText = styled(Typography)({
  fontSize: '24px',
  fontWeight: 'bold',
  letterSpacing: '4px',
  background: 'linear-gradient(90deg, #00F0FF 0%, #B026FF 100%)',
  WebkitBackgroundClip: 'text',
  WebkitTextFillColor: 'transparent',
  backgroundClip: 'text',
  textShadow: '0 0 20px rgba(0, 240, 255, 0.5)',
});

const Tagline = styled(Typography)(({ theme }) => ({
  fontSize: '10px',
  letterSpacing: '2px',
  color: 'rgba(176, 38, 255, 0.8)',
  textTransform: 'uppercase',
  textShadow: '0 0 10px rgba(176, 38, 255, 0.5)',
}));

export const LogoGradient: FC = () => {
  return (
    <LogoContainer>
      <AsciiContainer>
        <svg width="560" height="168" viewBox="0 0 560 168" xmlns="http://www.w3.org/2000/svg">
          <rect width="560" height="168" fill="#000000" />
          <text x="0" y="16" fontFamily="monospace" fontSize="14" fill="#cd00cd">
            <tspan x="0" dy="0">     $$$$$$$                      </tspan>
          </text>
          <text x="0" y="32" fontFamily="monospace" fontSize="14" fill="#cd00cd">
            <tspan x="0" dy="0">     $$  __$$                     </tspan>
          </text>
          <text x="0" y="48" fontFamily="monospace" fontSize="14">
            <tspan x="0" dy="0" fill="#cd00cd">     $$ |  $$ | $$$$$$   $$$$$$  $$ |  $$  $$$$$$   $$$$$$$  $$$$$$</tspan>
          </text>
          <text x="0" y="64" fontFamily="monospace" fontSize="14">
            <tspan x="0" dy="0" fill="#cd00cd">     $$$$$$$  |$$  __$$   _______ $$ | $$  | _______ $$  _____| _______</tspan>
          </text>
          <text x="0" y="80" fontFamily="monospace" fontSize="14">
            <tspan x="0" dy="0" fill="#cd00cd">     $$  ____/ $$ |  __| $$$$$$$ |$$$$$$  /  $$$$$$$ |$_______  $$$$$$$ |</tspan>
          </text>
          <text x="0" y="96" fontFamily="monospace" fontSize="14">
            <tspan x="0" dy="0" fill="#cd00cd">     $$ |      $$ |      $$  __$$ |$$  _$$&lt;  $$  __$$ | ______  $$  __$$ |</tspan>
          </text>
          <text x="0" y="112" fontFamily="monospace" fontSize="14">
            <tspan x="0" dy="0" fill="#cd00cd">     $$ |      $$ |      $$$$$$$$ |$$ |   $  $$$$$$$$ |$$$$$$$  |$$$$$$$ |</tspan>
          </text>
          <text x="0" y="128" fontFamily="monospace" fontSize="14" fill="#cd00cd">
            <tspan x="0" dy="0">     __|      __|       _______|__|  __|  _______|_______/  _______|</tspan>
          </text>
          <text x="0" y="152" fontFamily="monospace" fontSize="14" fill="#ffff00">
            <tspan x="280" dy="0">by HETU Protocol</tspan>
          </text>
        </svg>
      </AsciiContainer>
      <Stack spacing={0.5} alignItems="center">
        <LogoText>PRAKASA</LogoText>
        <Tagline>Decentralized Intelligence</Tagline>
      </Stack>
    </LogoContainer>
  );
};
