# Estado de provisionamento

Este diretório reserva o conceito de estado no repositório. O estado efetivo é
gravado por instalação em `ComfyUI/user/default/EDx/provisioning-state.json`,
para não compartilhar dados da máquina do usuário no Git. O provisionador
sempre revalida arquivos e marcadores do filesystem antes de confiar nele.
